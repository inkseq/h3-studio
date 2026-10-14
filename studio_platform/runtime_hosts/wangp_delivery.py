"""Explicit export/upload of an existing sealed operation on its GPU host.

No business SDK, cloud credentials, model calls or process-global environment changes.
"""
from __future__ import annotations
import base64
from fractions import Fraction
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import ssl
import time
from urllib.parse import urlsplit
from ..media import ffmpeg, probe
from ..inference.protocol import BackendError, NotReady
from ..inference.wangp_delivery import (MAX_BYTES, POLICY, validate_delivery_request,
    validate_delivery_receipt, validate_role_descriptor, validate_upload_target, json_copy)
from .wangp_receipts import checked_directory, checked_reader, sync_directory

def _validate_native_timing(stream, delivery):
    """Reject an unexpected native timeline instead of coercing its frame rate."""
    try:
        if (Fraction(stream["avg_frame_rate"]) != delivery["fps"]
                or int(stream["nb_frames"]) != delivery["frame_count"]
                or abs(float(stream["duration"])-delivery["duration_s"]) > .001
                or abs(float(stream.get("start_time", 0))) > .001):
            raise ValueError
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise BackendError("native_output_timing_mismatch") from None


def _validate_video(path, width, height, duration, audio, *, timeout=180):
    data = probe(path)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        fps = float(Fraction(stream["avg_frame_rate"]))
        actual = float(data["format"]["duration"])
        if ((stream["width"], stream["height"]) != (width, height) or abs(fps-24) > .001
            or abs(actual-duration) > .1 or stream.get("codec_name") != "h264"
            or int(stream.get("nb_frames", 0)) != round(duration*24)):
            raise ValueError
    except (ValueError, KeyError, ZeroDivisionError):
        raise BackendError("output_video_verification_failed") from None
    if audio:
        _validate_audio(path, duration, timeout=timeout)
    elif any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise BackendError("unexpected_output_audio")
    ffmpeg(["-xerror", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-threads", "1", "-f", "null", "-"], timeout=timeout)
    return {"width": width, "height": height, "duration_s": duration, "fps": 24, "has_audio": audio}


def _validate_audio(path, duration, *, flac=False, timeout=180):
    """Verify the requested tolerance and return the file's observed duration."""
    data = probe(path)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        actual = float(stream.get("duration", data["format"].get("duration")))
        if (int(stream["sample_rate"]) != 32000 or stream["channels"] != 2
            or not math.isfinite(actual) or abs(actual-duration) > .1
            or flac and stream.get("codec_name") != "flac"):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise BackendError("output_audio_verification_failed") from None
    ffmpeg(["-xerror", "-i", path, "-map", "0:a:0", "-threads", "1", "-f", "null", "-"], timeout=timeout)
    return actual

def file_descriptor(kind,path,root,evidence):
    size=0;sha=hashlib.sha256();md5=hashlib.md5()
    with checked_reader(path,root) as stream:
        while chunk:=stream.read(1024*1024):
            size+=len(chunk)
            if size>MAX_BYTES:raise BackendError('wangp_delivery_size_invalid')
            sha.update(chunk);md5.update(chunk)
    return {'kind':kind,'size_bytes':size,'sha256':sha.hexdigest(),
        'content_md5':base64.b64encode(md5.digest()).decode(),'content_type':'video/mp4' if kind=='video' else 'audio/flac','evidence':evidence}

class GPUDelivery:
    def __init__(self,sealed_root,*,exporter=None,uploader=None,clock=time.time):
        self.root=Path(sealed_root)
        self.exporter=exporter or self._export
        self.uploader=uploader or self._put
        self.clock=clock

    def _folder(self,op):
        return checked_directory(self.root/op/'delivery',create=True)

    def _verify_roles(self,value):
        folder=self._folder(value['identity']['operation_id'])
        for kind,role in value['roles'].items():
            path=folder/('verified.mp4' if kind=='video' else 'verified.flac')
            if file_descriptor(kind,path,self.root,role['evidence'])!=role:
                raise BackendError('wangp_delivery_export_changed')

    def prepare(self,request,original,context,journal):
        request=validate_delivery_request(request)
        op=request['identity']['operation_id'];old=journal.delivery(op)
        if old is not None:
            if old['fingerprint']!=request['fingerprint']:raise NotReady('wangp_delivery_fingerprint_mismatch')
            if old['state']!='failed':
                if old['roles']:self._verify_roles(old)
                return old
        output=context['output_spec'];spec=request['delivery_spec']
        if output.get('frames')!=spec['frame_count'] or output.get('actual_duration')!=spec['duration_s']:
            raise BackendError('wangp_delivery_context_mismatch')
        width,height=output.get('width'),output.get('height')
        if any(type(v) is not int or not 1<=v<=16384 for v in (width,height)):
            raise BackendError('wangp_delivery_geometry_invalid')
        value={**request,'state':'preparing','roles':{},'objects':{},'stage_durations':{}}
        journal.save_delivery(op,value)
        start=time.monotonic()
        try:
            roles=self.exporter(request,original,width,height,self._folder(op))
            if set(roles)!=({'video','audio'} if request['generate_audio'] else {'video'}):
                raise BackendError('wangp_delivery_roles_invalid')
            for kind,role in roles.items():validate_role_descriptor(kind,role,delivery_spec=spec,generate_audio=request['generate_audio'])
            value.update(state='prepared',roles=roles,stage_durations={'encode_validate_seconds':time.monotonic()-start})
            self._verify_roles(value)
            return journal.save_delivery(op,value)
        except Exception:
            value.update(state='failed',error_code='wangp_delivery_encoding_failed')
            journal.save_delivery(op,value)
            raise BackendError('wangp_delivery_encoding_failed') from None

    def _source(self,original,kind,folder):
        descriptor=next((r for r in original.artifacts if r.kind==kind),None)
        if descriptor is None:raise BackendError('wangp_delivery_source_missing')
        source=self.root/original.operation_id/(kind+'-'+descriptor.sha256+'.bin')
        suffix='.mp4' if descriptor.media_type=='video/mp4' else '.wav' if descriptor.media_type=='audio/wav' else '.flac'
        target=folder/('source-'+kind+suffix)
        if target.exists():
            sha=hashlib.sha256();size=0
            with checked_reader(target,self.root) as stream:
                while chunk:=stream.read(1024*1024):sha.update(chunk);size+=len(chunk)
            if (size,sha.hexdigest())!=(descriptor.size_bytes,descriptor.sha256):raise BackendError('wangp_delivery_source_changed')
            return target
        temporary=folder/(target.name+'.part');created=False
        try:
            sha=hashlib.sha256();size=0
            with checked_reader(source,self.root) as stream,temporary.open('xb') as out:
                created=True
                os.chmod(temporary,0o600)
                while chunk:=stream.read(1024*1024):
                    size+=len(chunk)
                    if size>descriptor.size_bytes or size>MAX_BYTES:raise BackendError('wangp_delivery_source_changed')
                    sha.update(chunk);out.write(chunk)
                out.flush();os.fsync(out.fileno())
            if (size,sha.hexdigest())!=(descriptor.size_bytes,descriptor.sha256):raise BackendError('wangp_delivery_source_changed')
            os.replace(temporary,target);sync_directory(folder)
            return target
        finally:
            if created and temporary.exists():temporary.unlink()

    def _export(self,request,original,width,height,folder):
        video=self._source(original,'video',folder)
        audio=self._source(original,'audio',folder) if request['generate_audio'] else None
        spec=request['delivery_spec'];duration=spec['duration_s']
        raw=probe(video);stream=next((s for s in raw.get('streams',[]) if s.get('codec_type')=='video'),{})
        if (stream.get('width'),stream.get('height'))!=(width,height):raise BackendError('output_dimensions_mismatch')
        _validate_native_timing(stream,spec)
        if audio is not None:_validate_audio(audio,duration)
        args=['-i',video]
        if audio is not None:args+=['-i',audio,'-map','0:v:0','-map','1:a:0']
        args+=['-c:v','libx264','-preset','veryfast','-crf',request['export_crf'],'-pix_fmt','yuv420p','-fps_mode','passthrough']
        args+=['-c:a','aac','-ar','32000','-ac','2'] if audio is not None else ['-an']
        final=folder/'verified.mp4'
        ffmpeg([*args,'-threads','1','-fs',MAX_BYTES+1,'-movflags','+faststart',final],timeout=180)
        evidence=_validate_video(final,width,height,duration,audio is not None,timeout=180)
        observed=probe(final);vs=next(s for s in observed['streams'] if s.get('codec_type')=='video')
        _validate_native_timing(vs,spec)
        evidence.update(delivery_spec=spec,frame_count=spec['frame_count'],container_duration_s=float(observed['format']['duration']))
        if audio is not None:
            aus=next(s for s in observed['streams'] if s.get('codec_type')=='audio')
            evidence['audio_duration_s']=float(aus.get('duration',observed['format']['duration']))
        roles={'video':file_descriptor('video',final,self.root,evidence)}
        if audio is not None:
            final_audio=folder/'verified.flac'
            ffmpeg(['-i',audio,'-c:a','flac','-threads','1','-ar','32000','-ac','2','-fs',MAX_BYTES+1,final_audio],timeout=180)
            actual=_validate_audio(final_audio,duration,flac=True,timeout=180)
            roles['audio']=file_descriptor('audio',final_audio,self.root,{'duration_s':actual,'has_audio':True,'delivery_spec':spec})
        return roles

    def upload(self,original,targets,journal):
        op=original.operation_id;value=journal.delivery(op)
        if value is None or value['state'] not in {'prepared','uploading','uploaded','unknown','rejected'}:
            raise NotReady('wangp_delivery_not_prepared')
        if type(targets) is not dict or not targets or set(targets)-set(value['roles']):
            raise BackendError('wangp_delivery_targets_invalid')
        self._verify_roles(value)
        for kind,target in targets.items():
            validate_upload_target(kind,target,value,now=self.clock())
            prior=value['objects'].get(kind)
            if prior is not None:
                if prior['key']!=target['key'] or prior['storage_binding']!=target['storage_binding']:
                    raise NotReady('wangp_delivery_storage_binding_changed')
                if prior['state']=='uploaded':continue
                if prior['state'] in {'uploading','unknown'} or not prior['stop_proven']:
                    raise NotReady('wangp_delivery_upload_unknown')
        # Commit every fixed key, including later roles that have not begun PUT.
        for kind,target in targets.items():
            if kind not in value['objects']:
                value['objects'][kind]={'key':target['key'],'storage_binding':target['storage_binding'],'state':'not_started','stop_proven':True}
        journal.save_delivery(op,value)
        start=time.monotonic()
        for kind in ('video','audio'):
            if kind not in targets:continue
            target=targets[kind]
            prior=value['objects'].get(kind)
            if prior is not None and prior['state']=='uploaded':continue
            value['objects'][kind]={'key':target['key'],'storage_binding':target['storage_binding'],'state':'uploading','stop_proven':False}
            value['state']='uploading';value.pop('error_code',None)
            journal.save_delivery(op,value)  # Commit PUT intent before any network call.
            path=self._folder(op)/('verified.mp4' if kind=='video' else 'verified.flac')
            outcome=self.uploader(path,target,value['roles'][kind])
            if outcome not in {'uploaded','rejected','unknown'}:outcome='unknown'
            value['objects'][kind].update(state=outcome,stop_proven=outcome in {'uploaded','rejected'})
            value['stage_durations']['upload_seconds']=value['stage_durations'].get('upload_seconds',0)+time.monotonic()-start
            states={v['state'] for v in value['objects'].values()}
            value['state']=('uploaded' if set(value['objects'])==set(value['roles']) and states=={'uploaded'} else
                'uploading' if 'uploading' in states else 'unknown' if 'unknown' in states else 'rejected' if 'rejected' in states else 'prepared')
            if outcome in {'rejected','unknown'}:value['error_code']='wangp_delivery_upload_'+outcome
            journal.save_delivery(op,value)
            if outcome!='uploaded':break
            start=time.monotonic()
        return validate_delivery_receipt(value)

    def _put(self,path,target,role):
        parsed=urlsplit(target['url']);deadline=time.monotonic()+180
        connection=http.client.HTTPSConnection(parsed.hostname,port=443,timeout=30,context=ssl.create_default_context())
        class Body:
            def __init__(self,source):self.source=source;self.count=0;self.sha=hashlib.sha256();self.md5=hashlib.md5()
            def read(self,n=8192):
                if time.monotonic()>deadline:raise TimeoutError
                chunk=self.source.read(min(n,1024*1024));self.count+=len(chunk)
                if self.count>role['size_bytes']:raise ValueError
                self.sha.update(chunk);self.md5.update(chunk);return chunk
        try:
            with checked_reader(path,self.root) as source:
                body=Body(source)
                connection.request('PUT',parsed.path+'?'+parsed.query,body=body,headers=target['headers'])
                if (body.count,body.sha.hexdigest(),base64.b64encode(body.md5.digest()).decode())!=(role['size_bytes'],role['sha256'],role['content_md5']):
                    return 'unknown'
                response=connection.getresponse();raw=response.read(8193)
                if len(raw)>8192 or getattr(response,'length',None) not in (None,0):return 'unknown'
                if response.status in {200,201,204}:return 'uploaded'
                if response.status in {400,401,403,412}:return 'rejected'
                return 'unknown'
        except Exception:return 'unknown'
        finally:connection.close()

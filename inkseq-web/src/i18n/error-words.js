// English for errors written in Chinese at the point they are thrown (#1017). The Chinese
// text is the key, gettext style, so each throw reads as it always did.
import { readBrowserLocale } from './locale.js';

const ENGLISH = {
  '请先核对登录账户和当前创作，再下载原文件。':
    'Check your account and current work first, then download the original file.',
  '产物标识无效，请刷新原任务后下载。':
    "This file's ID is not valid. Refresh the original task, then download it.",
  '产物下载地址与原任务不一致，请刷新原记录。':
    'This download link does not match the original task. Refresh the original record.',
  '账户已经改变，请从当前创作重新下载原文件。':
    'Your account changed. Download the original file again from your current work.',
  '账户或创作已经改变，请从当前创作重新下载原文件。':
    'Your account or work changed. Download the original file again from your current work.',
  '当前操作正在核对，请稍后下载原文件。':
    'An action is still being checked. Download the original file in a moment.',
  'SRT时间应小于100小时。': 'SRT times must be under 100 hours.',
  '请先核对并确认这一版字幕文字和时间。':
    'Check and confirm this version of the caption text and timing first.',
  '账户或项目已经切换；迟到的响应没有写入当前作品。':
    'Your account or project changed. The late response was not added to your current work.',
  '请先登录并打开一个云项目。': 'Sign in and open a cloud project first.',
  '浏览器无法保存提交标识；为避免重复提交，请先恢复存储。':
    'Your browser could not save the submission ID. To avoid a duplicate, restore browser storage first.',
  '未确认任务的记录无法读取。请保留浏览器数据并先核对服务任务。':
    'The record of an unconfirmed task could not be read. Keep your browser data and check the task first.',
  '无法保留提交记录，未提交任务。':
    'The submission record could not be kept, so nothing was submitted.',
  '未确认批次记录无法读取，请先核对云端任务。':
    'The record of an unconfirmed batch could not be read. Check your tasks first.',
  '无法保留批次标识，未提交。': 'The batch ID could not be kept, so nothing was submitted.',
  '未确认粗剪记录无法读取，请保留浏览器数据并核对任务。':
    'The record of an unconfirmed rough cut could not be read. Keep your browser data and check the task.',
  '无法保存粗剪提交标识，未提交。':
    'The rough cut ID could not be saved, so nothing was submitted.',
  '云素材标识无效。': "This cloud file's ID is not valid.",
  '服务没有返回可核对的账户身份。': 'The service did not return an account we could check.',
  '上一项云操作还在处理中。': 'The previous cloud action is still running.',
  '账户已经改变，旧账户列表没有写入。':
    "Your account changed. The old account's list was not shown.",
  '素材尚未就绪或属于其他项目，未加入素材库。':
    'This file is not ready or belongs to another project, so it was not added to the library.',
  '先登录云工作室。': 'Sign in to the cloud studio first.',
  '本机云草稿基于较旧版本。请下载草稿或保存副本；未覆盖云端。':
    'This draft is based on an older version. Download it or save a copy; the cloud version was not overwritten.',
  '另一处已更新这个项目。当前草稿已保留；请保存为云副本或下载后比较，未覆盖远端。':
    'This project was updated elsewhere. Your draft is kept. Save it as a copy or download it to compare; the cloud version was not overwritten.',
  '素材尚未处理完成，未当作可生成的输入。':
    'This file is still processing, so it was not used as an input.',
  '参考素材不存在。': 'The reference file does not exist.',
  '上传时原素材已更换，请重新预检。': 'The original file changed during upload. Check again.',
  '原任务恢复后待核对，请等待管理员核对执行与费用；可以继续编辑作品，不能重新提交。':
    'The original task is waiting for review after recovery. You can keep editing, but cannot submit it again yet.',
  '截帧收据尚未确认；未加入档案或替代原参考。':
    'The frame capture is not confirmed yet, so it was not added or used as a reference.',
  '原提交尚未核对，请先刷新原记录。':
    'The original submission is not confirmed yet. Refresh the original record first.',
  '镜头不存在。': 'This shot does not exist.',
  '请先保存分镜配置。': 'Save the shot settings first.',
  '所选模型与草稿/精修配置不一致或尚不可用；未使用另一模型替代。':
    'The chosen quality does not match this draft or is not available. Nothing else was used instead.',
  '这一镜已有任务排队或执行，请查看原任务。':
    'This shot already has a task queued or running. See the original task.',
  '请先选定上一镜的真实成片；不会使用未选定候选。':
    "Choose the previous shot's finished video first. Unchosen takes are not used.",
  '截帧期间上一镜的选择已改变，请重新预检。':
    "The previous shot's choice changed during frame capture. Check again.",
  '原选定成片的请求尚未确认，不能用当前编辑草稿代替。':
    "The chosen video's original request is not confirmed yet, so the current draft cannot stand in for it.",
  '精修必须使用当前选定成片的原请求；选择已改变，请重新预检。':
    "Refining must use the chosen video's original request. The choice changed, so check again.",
  '精修模型或模式与原选定请求不一致。':
    "The refine settings do not match the chosen video's original request.",
  '服务端模型档案与界面所选不一致，未提交。':
    "The server's settings do not match what you chose, so nothing was submitted.",
  '预检期间镜头或档案已改变，旧计划未当作可提交计划。':
    'The shot or its settings changed during the check. The old plan cannot be submitted.',
  '生成计划响应无效。': 'The generation plan response was not valid.',
  '实际预检模型或模式与所选配置不一致，未提交。':
    'The checked settings do not match what you chose, so nothing was submitted.',
  '这个镜头有未确认的提交。先刷新任务或用原标识核对提交，不能创建新计划绕过。':
    'This shot has an unconfirmed submission. Refresh your tasks first; a new plan cannot replace it.',
  '这个镜头的相同版本已在队列中，请查看进度或先取消原任务。':
    'The same version of this shot is already queued. See its progress, or cancel the original task first.',
  '此生成配方已不可用，请刷新并重新选择。':
    'These generation settings are no longer available. Refresh and choose again.',
  '自动匹配云端设置未保存，请重新预检。':
    'The automatic cloud settings were not saved. Check again.',
  '选段仍在处理中，请稍后重新预检；未使用整个原文件替代。':
    'The selected range is still processing. Check again shortly; the whole file was not used instead.',
  '时间锚点的原文件已改变或选段无效，请重设锚点。':
    'The original file for this time anchor changed or the range is not valid. Set the anchor again.',
  '锚点选段尚未就绪，没有使用完整原文件替代。':
    'The anchor range is not ready. The whole file was not used instead.',
  '服务返回了无效的生成计划。': 'The service returned a generation plan that was not valid.',
  '请先完成这个镜头的预检，并解决阻塞项。':
    'Finish the check for this shot and fix what is blocking it first.',
  '当前服务未启用生成；未提交任务。':
    'Generation is not switched on for this service. Nothing was submitted.',
  '镜头或参考已改变，请重新预检。': 'The shot or its references changed. Check again.',
  '计划已过期，请重新预检。': 'This plan has expired. Check again.',
  '任务正在刷新，请稍后加载更早记录。': 'Tasks are refreshing. Load earlier records in a moment.',
  '此镜头属于待核对批次，请先恢复原批次。':
    'This shot is part of a batch waiting for review. Recover the original batch first.',
  '已有未确认的批次，请先恢复原批次。':
    'A batch is still unconfirmed. Recover the original batch first.',
  '有镜头正在核对提交，不能重复加入批次。':
    'A shot is still being checked, so it cannot be added to a batch again.',
  '先选择需要生成的镜头。': 'Choose the shots to generate first.',
  '本章有未确认的粗剪提交，先恢复原任务，不能创建新计划绕过。':
    'This chapter has an unconfirmed rough cut. Recover the original task first; a new plan cannot replace it.',
  '本章已有粗剪正在排队或执行，请查看原任务。':
    'This chapter already has a rough cut queued or running. See the original task.',
  '当前服务尚未提供章节粗剪，未提交任务。':
    'Chapter rough cuts are not available on this service yet. Nothing was submitted.',
  '当前服务不支持已确认字幕烧录。请刷新能力或升级服务；没有静默导出无字幕版本。':
    'This service cannot burn in confirmed captions. Refresh or upgrade; a version without captions was not exported.',
  '当前服务尚不支持已保存的剪辑选段。请升级服务，或明确恢复从视频开头取片；未静默忽略入点。':
    'This service cannot use saved trim ranges yet. Upgrade it, or choose to start from the beginning of the video.',
  '当前服务尚不支持同次生成声音绑定，未忽略绑定按普通音轨导出。请升级服务，或明确静音／解除关联后再预检。':
    'This service cannot use bound generated sound yet. The export did not ignore it. Upgrade it, or choose another track.',
  '请先选择一个章节。': 'Choose a chapter first.',
  '保存期间时间线又有修改，请重新预检。': 'The timeline changed while saving. Check again.',
  '素材同步后字幕依据有变化，请重新核对字幕。':
    'The captions changed after files were synced. Check the captions again.',
  '粗剪计划响应无效，没有当作可执行计划。':
    'The rough cut plan response was not valid, so it was not used.',
  '服务未确认字幕将烧入本次成片，未接受这个可执行计划。请刷新服务能力后重新预检。':
    'The service did not confirm captions would be burned in. Refresh and check again.',
  '请先完成本章粗剪预检。': 'Finish the rough cut check for this chapter first.',
  '服务尚未启用章节粗剪，未提交任务。':
    'Chapter rough cuts are not switched on for this service. Nothing was submitted.',
  '章节时间线或素材已改变，请重新预检。': 'The chapter timeline or files changed. Check again.',
  '粗剪计划已过期，请重新预检。': 'This rough cut plan has expired. Check again.',
  '登录期间会话已经改变，请重新核对。':
    'Your session changed during sign-in. Please check and try again.',
  '登录响应缺少账户身份。': 'The sign-in response did not include an account.',
  '请重新核对当前账户与上次作品。': 'Check your current account and last work again.',
  '任务不属于当前项目。': 'This task does not belong to the current project.',
  '镜头已被删除。': 'This shot has been deleted.',
  '先选择有明确能力说明的配方。':
    'Choose settings with a clear description of what they support first.',
  '回收站收据不完整或与原选择不一致，请核对原操作。':
    'The trash receipt is incomplete or does not match your selection. Check the original action.',
  '请选择当前可操作的创作；任务未结束或删除中的记录不能选择。':
    'Choose work you can act on. Unfinished tasks and items being deleted cannot be chosen.',
  '镜头所属章节不存在。': "This shot's chapter does not exist.",
  '本章已达32条音轨上限，请先整理或移除不需要的音轨。':
    'This chapter has reached the limit of 32 audio tracks. Remove ones you do not need first.',
  '先选择要交接的章节。': 'Choose the chapter to hand over first.',
  '造型已删除，请重新选择后上传。': 'This look was deleted. Choose again, then upload.',
  '上传期间这张造型参考已另选；保留你的新选择，没有覆盖。':
    'You chose another reference for this look during upload. Your new choice was kept.',
  '这个地点已经不存在，请重新选择。': 'This place no longer exists. Choose again.',
  '这个角色已经不存在，请重新选择。': 'This character no longer exists. Choose again.',
  '这种内容不支持直接上传文件。': 'This kind of content cannot be uploaded directly.',
  '请登录云工作室后读取此素材。': 'Sign in to the cloud studio to read this file.',
  '素材存储读取已中止。': 'Reading from file storage was stopped.',
  '请选择 PNG / JPG / WebP / GIF 图片、MP4 / WebM / MOV 视频或 MP3 / WAV / M4A / OGG 音频。':
    'Choose a PNG, JPG, WebP or GIF image, an MP4, WebM or MOV video, or an MP3, WAV, M4A or OGG audio file.',
  '文件为空，请换一个文件。': 'This file is empty. Choose another one.',
  '云上传尚未就绪，请重新核对登录后重试；未把文件改存到本机。':
    'Cloud upload is not ready. Check your sign-in and try again; the file was not saved to this device instead.',
  '项目结构超过 8 MB，请拆分项目后再备份；当前项目未改变。':
    'The project structure is over 8 MB. Split the project, then back it up; nothing was changed.',
  '素材标识不合法，请重新上传这份素材。': "This file's ID is not valid. Upload it again.",
  '备份包含超过 30 MB 的素材，请先压缩或替换这份素材。':
    'The backup has a file over 30 MB. Compress or replace it first.',
  '完整备份的不同素材合计超过 120 MB，无法生成可恢复的备份。请将项目拆分，或压缩并替换较大素材后重试；没有素材被删除。':
    'The files in this backup total over 120 MB, so it could not be restored. Split the project or compress the files.',
  '备份超过 150 MB，无法在本原型恢复。请拆分项目后重试；当前项目未改变。':
    'The backup is over 150 MB and cannot be restored here. Split the project and try again; nothing was changed.',
  '备份超过 150 MB，请在原项目中拆分或压缩素材后重新备份。':
    'The backup is over 150 MB. Split or compress the original project, then back it up again.',
  '项目结构超过 8 MB，无法导入，请拆分项目。':
    'The project structure is over 8 MB and cannot be imported. Split the project.',
  '备份包含过多文件，请使用本工作室导出的备份。':
    'This backup has too many files. Use a backup exported from this studio.',
  '备份中没有 project.json。': 'This backup has no project.json.',
  '素材标识不合法。': "This file's ID is not valid.",
  '不同素材合计超过 120 MB，请拆分项目后重新备份。':
    'The files total over 120 MB. Split the project, then back it up again.',
  '登录账户已改变，原账户响应未展示。':
    "The signed-in account changed, so the previous account's response was not shown.",
  '浏览器无法保留操作标识，未发出管理操作。':
    'Your browser could not keep the action ID, so the admin action was not sent.',
  '管理身份与登录账户不一致。': 'The admin identity does not match the signed-in account.',
  '原操作仍在处理中。': 'The original action is still running.',
  '有原管理操作待核对，请先核对原操作。':
    'An admin action is waiting to be checked. Check it first.',
  '供应商库存刷新尚未确认，请稍后重新查询。':
    'The supplier stock refresh is not confirmed yet. Look it up again shortly.',
  '库存响应与当前模型或模式不一致，请重新查询。':
    'The stock response does not match the current settings. Look it up again.',
  '供应商库存刷新尚未返回本轮记录，请重新查询。':
    "The supplier stock refresh has not returned this round's record. Look it up again.",
  '请先核对原管理操作。': 'Check the original admin action first.',
  '原操作记录无法核对，请保留记录并联系管理员。':
    "The original action's record could not be checked. Keep it and contact an administrator.",
  '创作已被更新或目标已改变。请核对最新材料后再调整，原素材保留。':
    'The work was updated or its target changed. Check the latest material before adjusting; the original files are kept.',
  '工作账户、会话或内容已经改变；旧响应没有加入当前创作。':
    'The account, session or content changed. The old response was not added to your current work.',
  '执行状态已被更新，请重新核对。': 'The run status was updated. Please check again.',
  '有原操作结果待核对，请先恢复原操作，不要重复提交。':
    "An action's result is waiting to be checked. Recover it first, and do not submit again.",
  '无法保存原操作标识，未提交。请启用浏览器存储后重试。':
    'The action ID could not be saved, so nothing was submitted. Turn on browser storage and try again.',
  '创作列表类型不正确。': 'The list type is not correct.',
  '筛选已改变，旧列表未加入。': 'The filter changed, so the old list was not added.',
  '原操作已变化，请核对原记录。': 'The original action changed. Check the original record.',
  '原清理操作无法核对，请返回原账户。':
    'The original cleanup could not be checked. Return to the original account.',
  '归档或测试标记收据不完整，保留原操作供核对。':
    'The archive or test mark receipt is incomplete. The original action is kept for checking.',
  '原标记目标不完整，请核对原操作。':
    "The original mark's target is incomplete. Check the original action.",
  '旧版本读取已丢弃，请刷新最新内容。':
    "The old version's read was dropped. Refresh for the latest content.",
  '会话响应不匹配。': 'The session response did not match.',
  '这条创作已在回收站或删除处理中，请先核对或恢复原记录。':
    'This work is already in the trash or being deleted. Check or restore the original record first.',
  '原操作仍在处理中，请等待。': 'The original action is still running. Please wait.',
  '列表类型不正确。': 'The list type is not correct.',
  '请等待原操作完成。': 'Please wait for the original action to finish.',
  '请先核对原操作，未建立新的清理确认。':
    'Check the original action first. No new cleanup was set up.',
  '请在对应列表重新选择。': 'Choose again in the matching list.',
  '账户、筛选或选择已改变，请重新预览。':
    'The account, filter or selection changed. Preview again.',
  '请先核对原操作，不要再次确认。': 'Check the original action first, and do not confirm again.',
  '预览已失效或含不可操作的记录，请重新核对。':
    'The preview has expired or includes items you cannot act on. Please check again.',
  '所选记录已更新，请重新预览。': 'The chosen records were updated. Preview again.',
  '先核对原操作。': 'Check the original action first.',
  '直接出卡只用于原输入模式，不替代助手回复。':
    'Direct cards only work in the original input mode and do not replace an assistant reply.',
  '原操作记录无法读取，请管理员核对，不要重新创建任务。':
    "The original action's record could not be read. Ask an administrator to check it, and do not create a new task.",
  '请登录原操作账户。': 'Sign in to the account that started this action.',
  '请先打开原操作会话。': 'Open the original session first.',
  '原上传收据暂未查到，保持原标识；未自动重新上传。请管理员核对后恢复。':
    'The original upload receipt was not found yet. The ID is kept and nothing was uploaded again. Ask an administrator to check it.',
  '无法保存输入草稿，尚未发送。': 'Your draft could not be saved, so it was not sent.',
  '镜头已不存在。': 'This shot no longer exists.',
  '旧项目结构不完整，原始数据已保留。':
    "This older project's structure is incomplete. The original data is kept.",
  '请先返回本机作品，再操作本机故事列表。':
    'Return to your work on this device first, then use its story list.',
  '本机故事列表无法读取，请先下载列表原始数据和当前作品备份。':
    'The story list on this device could not be read. Download its raw data and a backup of your current work first.',
  '上一项云操作还在处理中，请稍后切换故事。':
    'The previous cloud action is still running. Switch stories in a moment.',
  '本机故事列表无法读取，请先下载备份。':
    'The story list on this device could not be read. Download a backup first.',
  '这个本机故事已不在列表中，请刷新故事列表。':
    'This story is no longer in the list on this device. Refresh the list.',
  '原本机作品暂时无法恢复，请先备份当前作品。':
    'Your work on this device cannot be restored right now. Back up your current work first.',
  '请填写故事名称。': 'Enter a story name.',
  '上一项云操作还在处理中，请稍后新建故事。':
    'The previous cloud action is still running. Create the story in a moment.',
  '采用的视频已经改变，未保存旧来源选段。':
    'The video in use changed, so the old source range was not saved.',
  '对齐24fps后选段不足一帧。': 'The range is shorter than one frame at 24 fps.',
  '一个镜头最长3600秒，请拆分镜头。': 'A shot can be at most 3600 seconds. Split it.',
  '账户或作品已经切换；迟到的素材结果没有写入当前作品，已保存的原文件不会被删除。':
    'Your account or work changed. The late file result was not added to your current work; saved originals are not deleted.',
};

// Outside a page (tests, server rendering) the source text stands.
export function words(text) {
  if (typeof globalThis.document === 'undefined') return text;
  try {
    return readBrowserLocale() === 'zh' ? text : ENGLISH[text] || text;
  } catch {
    return text;
  }
}

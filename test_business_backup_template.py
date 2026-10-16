"""Offline regressions for the backup policy boundary, not an IAM simulator.

Only the template's small condition subset is interpreted. Unknown operators
fail the tests; actual CloudFormation/IAM acceptance remains a deployment gate.
No SDK, AWS credentials or network are used.
"""
import fnmatch
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch


TEMPLATE = json.loads((Path(__file__).parent / "deploy/platform/business-backup.json").read_text())
VALUES = {
    "AWS::Partition": "aws", "AWS::AccountId": "123456789012",
    "AWS::Region": "ap-southeast-1", "AWS::URLSuffix": "amazonaws.com",
    "AWS::StackId": "arn:aws:cloudformation:ap-southeast-1:123456789012:stack/backup/unique-id",
    "BackupBucketName": "sixnine-business-backup-test",
    "BackupBucket": "sixnine-business-backup-test",
    "BackupBucket.Arn": "arn:aws:s3:::sixnine-business-backup-test",
    "BackupKey.Arn": "arn:aws:kms:ap-southeast-1:123456789012:key/test-key",
    "BackupRole": "generated-backup-role",
    "BackupRole.Arn": "arn:aws:iam::123456789012:role/generated-backup-role",
    "SourceInstanceRoleName": "sixnine-platform-ec2",
    "SourceInstanceRolePath": "",
    "ExpectedAccountId": "123456789012", "ExpectedRegion": "ap-southeast-1",
}


def resolve(value):
    if isinstance(value, list):
        return [resolve(v) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"Ref"}:
        return VALUES[value["Ref"]]
    if set(value) == {"Fn::GetAtt"}:
        return VALUES[".".join(value["Fn::GetAtt"])]
    if set(value) == {"Fn::Sub"}:
        return re.sub(r"\$\{([^}]+)\}", lambda m: VALUES[m[1]], value["Fn::Sub"])
    if set(value) == {"Fn::Split"}:
        delimiter, text = resolve(value["Fn::Split"])
        return text.split(delimiter)
    if set(value) == {"Fn::Select"}:
        index, choices = resolve(value["Fn::Select"])
        return choices[index]
    if set(value) == {"Fn::Join"}:
        delimiter, parts = resolve(value["Fn::Join"])
        return delimiter.join(parts)
    return {k: resolve(v) for k, v in value.items()}


def many(value):
    return value if isinstance(value, list) else [value]


def conditions_match(conditions, context):
    for op, pairs in conditions.items():
        for key, expected in pairs.items():
            actual = context.get(key)
            if op in ("StringEquals", "ArnEquals"):
                matched = actual is not None and actual in many(expected)
            elif op in ("StringNotEquals", "ArnNotEquals"):
                matched = actual not in many(expected)
            elif op in ("StringLike", "ArnLike"):
                matched = actual is not None and any(fnmatch.fnmatchcase(actual, p) for p in many(expected))
            elif op == "Bool":
                matched = actual is not None and str(actual).lower() == str(expected).lower()
            elif op == "NumericLessThan":
                matched = actual is not None and float(actual) < float(expected)
            else:
                raise AssertionError("Unreviewed condition: " + op)
            if not matched:
                return False
    return True


def statements_match(policy, effect, action, resource, context):
    return any(s["Effect"] == effect
               and any(fnmatch.fnmatchcase(action, a) for a in many(s["Action"]))
               and any(fnmatch.fnmatchcase(resource, r) for r in many(s["Resource"]))
               and conditions_match(s.get("Condition", {}), context)
               for s in policy["Statement"])


class BusinessBackupTemplateTests(unittest.TestCase):
    def setUp(self):
        self.resources = resolve(TEMPLATE["Resources"])
        self.access = self.resources["BackupAccessPolicy"]["Properties"]["PolicyDocument"]
        self.bucket_policy = self.resources["BackupBucketPolicy"]["Properties"]["PolicyDocument"]
        self.bucket = VALUES["BackupBucket.Arn"]
        self.key = VALUES["BackupKey.Arn"]
        self.object = self.bucket + "/business-backups/00000000-0000-4000-8000-000000000001/complete.json"
        self.context = {
            "aws:PrincipalArn": VALUES["BackupRole.Arn"], "aws:SecureTransport": True,
            "s3:TlsVersion": 1.2, "s3:if-none-match": "*",
            "s3:x-amz-server-side-encryption": "aws:kms",
            "s3:x-amz-server-side-encryption-aws-kms-key-id": self.key,
        }

    def allows(self, action, resource=None, context=None):
        resource = resource or self.object
        context = self.context if context is None else context
        return (statements_match(self.access, "Allow", action, resource, context)
                and not statements_match(self.bucket_policy, "Deny", action, resource, context))

    def test_encrypted_conditional_put_is_the_only_write_path(self):
        self.assertTrue(self.allows("s3:PutObject"))
        for key, wrong in [("s3:if-none-match", '"old-etag"'),
                           ("s3:x-amz-server-side-encryption", "AES256"),
                           ("s3:x-amz-server-side-encryption-aws-kms-key-id", "alias/backup")]:
            for value in (None, wrong):
                with self.subTest(key=key, value=value):
                    context = dict(self.context)
                    if value is None:
                        context.pop(key)
                    else:
                        context[key] = value
                    self.assertFalse(self.allows("s3:PutObject", context=context))
                    self.assertTrue(statements_match(self.bucket_policy, "Deny", "s3:PutObject", self.object, context))
        self.assertFalse(self.allows("s3:PutObject", self.bucket + "/release.zip"))
        self.assertFalse(self.allows("s3:PutObject", "arn:aws:s3:::other/business-backups/a"))

    def test_read_requires_backup_role_and_modern_tls_without_write_headers(self):
        for action in ("s3:GetObject", "s3:GetObjectVersion"):
            context = {k: v for k, v in self.context.items() if not k.startswith("s3:x-") and k != "s3:if-none-match"}
            self.assertTrue(self.allows(action, context=context))
            for changed in ({"aws:SecureTransport": False}, {"s3:TlsVersion": 1.1},
                            {"aws:PrincipalArn": "arn:aws:iam::123456789012:role/sixnine-platform-ec2"}):
                with self.subTest(action=action, changed=changed):
                    self.assertFalse(self.allows(action, context={**context, **changed}))

    def test_deletion_is_explicitly_denied_and_other_mutations_not_granted(self):
        for action in ("s3:DeleteObject", "s3:DeleteObjectVersion"):
            self.assertTrue(statements_match(self.bucket_policy, "Deny", action, self.object, self.context))
        granted = {a for s in self.access["Statement"] for a in many(s["Action"])}
        self.assertEqual(granted, {
            "s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls",
            "s3:GetEncryptionConfiguration", "s3:ListBucketVersions", "s3:PutObject",
            "s3:GetObject", "s3:GetObjectVersion", "kms:GenerateDataKey", "kms:Decrypt"})
        self.assertNotIn("s3:ObjectCreationOperation", json.dumps(self.bucket_policy))

    def test_listing_cannot_list_bucket_root_or_other_prefix(self):
        for prefix, allowed in [("business-backups/opaque/", True), ("", False),
                                ("business-backups/", False), ("releases/", False)]:
            self.assertEqual(self.allows("s3:ListBucketVersions", self.bucket,
                                        {**self.context, "s3:prefix": prefix}), allowed)
        self.assertFalse(self.allows("s3:ListBucket", self.bucket))

    def test_kms_requires_exact_object_context_and_s3_not_direct_decrypt(self):
        context = {"kms:ViaService": "s3.ap-southeast-1.amazonaws.com",
                   "kms:CallerAccount": "123456789012", "kms:EncryptionContext:aws:s3:arn": self.object}
        key_policy = self.resources["BackupKey"]["Properties"]["KeyPolicy"]
        crypt = [s for s in key_policy["Statement"] if s["Sid"] == "BackupRoleS3ObjectCryptography"][0]
        self.assertEqual(crypt["Principal"], {"AWS": VALUES["BackupRole.Arn"]})
        for action in ("kms:GenerateDataKey", "kms:Decrypt"):
            self.assertTrue(statements_match(self.access, "Allow", action, self.key, context))
            for change in ({"kms:ViaService": "ec2.ap-southeast-1.amazonaws.com"},
                           {"kms:ViaService": "s3.us-east-1.amazonaws.com"},
                           {"kms:CallerAccount": "000000000000"},
                           {"kms:EncryptionContext:aws:s3:arn": self.bucket},
                           {"kms:EncryptionContext:aws:s3:arn": self.bucket + "/other/x"}):
                with self.subTest(action=action, change=change):
                    self.assertFalse(statements_match(self.access, "Allow", action, self.key, {**context, **change}))
                    self.assertFalse(conditions_match(crypt["Condition"], {**context, **change}))
            self.assertFalse(statements_match(self.access, "Allow", action, self.key, {}))

    def test_host_gets_only_exact_assume_role_and_trust_requires_exact_session(self):
        host = self.resources["InstanceAssumeBackupRolePolicy"]["Properties"]
        self.assertEqual(host["Roles"], ["sixnine-platform-ec2"])
        self.assertEqual(host["PolicyName"], "sixnine-backup-assume-unique-id")
        self.assertEqual(len(host["PolicyDocument"]["Statement"]), 1)
        statement = host["PolicyDocument"]["Statement"][0]
        self.assertEqual((statement["Action"], statement["Resource"]), ("sts:AssumeRole", VALUES["BackupRole.Arn"]))
        role = self.resources["BackupRole"]["Properties"]
        self.assertNotIn("RoleName", role)
        self.assertEqual(role["MaxSessionDuration"], 3600)
        trust = role["AssumeRolePolicyDocument"]["Statement"]
        self.assertEqual(len(trust), 1)
        self.assertEqual(trust[0]["Principal"], {"AWS": "arn:aws:iam::123456789012:role/sixnine-platform-ec2"})
        for policy_statement in (statement, trust[0]):
            self.assertTrue(conditions_match(policy_statement["Condition"], {"sts:RoleSessionName": "sixnine-backup-copy"}))
            self.assertFalse(conditions_match(policy_statement["Condition"], {"sts:RoleSessionName": "app"}))
            self.assertFalse(conditions_match(policy_statement["Condition"], {}))

    def test_resource_protection_and_explicit_nonsecret_outputs(self):
        self.assertEqual(len(self.resources), 6)
        for resource in self.resources.values():
            self.assertEqual(resource["DeletionPolicy"], "Retain")
            self.assertEqual(resource["UpdateReplacePolicy"], "Retain")
        bucket = self.resources["BackupBucket"]["Properties"]
        self.assertTrue(all(bucket["PublicAccessBlockConfiguration"].values()))
        self.assertEqual(bucket["VersioningConfiguration"]["Status"], "Enabled")
        self.assertEqual(bucket["OwnershipControls"]["Rules"], [{"ObjectOwnership": "BucketOwnerEnforced"}])
        encryption = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        self.assertFalse(encryption["BucketKeyEnabled"])
        self.assertEqual(encryption["ServerSideEncryptionByDefault"], {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": self.key})
        self.assertNotIn("LifecycleConfiguration", bucket)
        self.assertNotIn("LoggingConfiguration", bucket)
        for parameter in TEMPLATE["Parameters"].values():
            self.assertNotIn("Default", parameter)
        for output in TEMPLATE["Outputs"].values():
            self.assertNotIn("Export", output)
        self.assertEqual(set(TEMPLATE["Outputs"]), {"BackupBucketName", "BackupKeyArn", "BackupRoleArn", "BackupPrefix", "BackupRegion", "BackupAccountId"})

    def test_resource_dependencies_have_no_cycle(self):
        def refs(node):
            found = set()
            if isinstance(node, list):
                for value in node:
                    found |= refs(value)
            elif isinstance(node, dict):
                if "Ref" in node:
                    found.add(node["Ref"])
                if "Fn::GetAtt" in node:
                    found.add(node["Fn::GetAtt"][0])
                if "Fn::Sub" in node:
                    found |= {name.split(".")[0] for name in re.findall(r"\$\{([^}]+)\}", node["Fn::Sub"])}
                for value in node.values():
                    found |= refs(value)
            return found & TEMPLATE["Resources"].keys()

        graph = {name: refs(r) for name, r in TEMPLATE["Resources"].items()}
        while graph:
            ready = {name for name, dependencies in graph.items() if not dependencies}
            self.assertTrue(ready, "Resource dependency cycle: " + str(graph))
            graph = {name: dependencies - ready for name, dependencies in graph.items() if name not in ready}

    def test_staging_role_path_does_not_expand_production_trust(self):
        role = TEMPLATE['Resources']['BackupRole']['Properties']['AssumeRolePolicyDocument']
        production = resolve(role)['Statement'][0]['Principal']['AWS']
        self.assertEqual(production, 'arn:aws:iam::123456789012:role/sixnine-platform-ec2')
        with patch.dict(VALUES, SourceInstanceRoleName='sixnine-staging-ec2', SourceInstanceRolePath='inkseq/staging/'):
            staging = resolve(role)['Statement'][0]['Principal']['AWS']
            self.assertEqual(staging, 'arn:aws:iam::123456789012:role/inkseq/staging/sixnine-staging-ec2')
            self.assertNotIn(production, json.dumps(resolve(role)))


if __name__ == "__main__":
    unittest.main()

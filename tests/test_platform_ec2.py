"""Offline checks for the explicitly reviewed small EC2 host profile."""
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'deploy' / 'platform' / 'ec2'


class EC2HostSpecTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((ROOT / 'launch-spec.json').read_text())['LaunchTemplateData']

    def test_no_ssh_or_container_instance_identity(self):
        self.assertNotIn('KeyName', self.spec)
        self.assertEqual(self.spec['MetadataOptions']['HttpTokens'], 'required')
        self.assertEqual(self.spec['MetadataOptions']['HttpPutResponseHopLimit'], 1)
        self.assertFalse(self.spec['NetworkInterfaces'][0]['AssociatePublicIpAddress'])

    def test_fixed_budget_and_retained_encrypted_storage(self):
        self.assertEqual(self.spec['InstanceType'], 't3a.medium')
        self.assertEqual(self.spec['CreditSpecification']['CpuCredits'], 'standard')
        self.assertTrue(self.spec['DisableApiTermination'])
        volume = self.spec['BlockDeviceMappings'][0]['Ebs']
        self.assertEqual((volume['VolumeSize'], volume['VolumeType']), (40, 'gp3'))
        self.assertTrue(volume['Encrypted'])
        self.assertFalse(volume['DeleteOnTermination'])

    def test_userdata_is_only_base_host(self):
        source = (ROOT / 'bootstrap-host.sh').read_text()
        self.assertIn('python3-boto3', source)
        self.assertIn('download.docker.com/linux/ubuntu', source)
        self.assertIn('systemctl disable --now ssh.service ssh.socket', source)
        self.assertIn('install -d -o 10001 -g 10001 -m 0700', source)
        self.assertIn('install -d -o 70 -g 70 -m 0700', source)
        self.assertNotIn('docker compose up', source)
        self.assertNotIn('get-secret-value', source)
        self.assertNotIn('usermod -aG docker', source)
        self.assertNotIn('sudoers', source)


if __name__ == '__main__':
    unittest.main()

"""A failed or ad-hoc Mac build must never qualify as a complete signed release."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('ibfetch_release',Path(__file__).resolve().parents[1]/'tools/verify_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


class ReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'proofs').mkdir()
        for target,suffix in release.TARGETS.items():
            name='ibfetch-1.6.0-'+target+suffix
            p=self.root/name;p.write_bytes(('synthetic-'+target).encode())
            digest=hashlib.sha256(p.read_bytes()).hexdigest()
            p.with_name(name+'.sha256').write_text(digest+'  '+name+'\n')
            if target.startswith('macos-'):
                (self.root/'proofs'/(target+'.json')).write_text(json.dumps(dict(developer_id=True,
                    hardened_runtime=True,notarization_ticket=True,gatekeeper=True,archive=name,sha256=digest)))

    def test_complete_release_writes_checksums_without_changing_archives(self):
        checks=release.verify_release(self.root,'1.6.0');self.assertEqual(len(checks),5)
        release.write_checksums(self.root,checks)
        self.assertEqual(len((self.root/'SHA256SUMS').read_text().splitlines()),5)
        self.assertEqual(release.verify_release(self.root,'1.6.0'),checks)

    def test_partial_or_changed_downloads_never_qualify(self):
        p=self.root/'ibfetch-1.6.0-windows-x64.zip';p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):release.verify_release(self.root,'1.6.0')
        p.unlink()
        with self.assertRaisesRegex(ValueError,'Missing release'):release.verify_release(self.root,'1.6.0')

    def test_notarization_reports_require_all_checks_and_the_exact_archive_hash(self):
        p=self.root/'proofs/macos-arm64.json';good=json.loads(p.read_text())
        for change in ({'gatekeeper':False},{'developer_id':1},{'sha256':'wrong'},{'archive':'old.zip'}):
            with self.subTest(change=change):
                p.write_text(json.dumps({**good,**change}))
                with self.assertRaisesRegex(ValueError,'Apple verification'):release.verify_release(self.root,'1.6.0')
        p.unlink()
        with self.assertRaises(OSError):release.verify_release(self.root,'1.6.0')

    def test_existing_release_records_are_never_replaced_with_other_archives(self):
        (self.root/'SHA256SUMS').write_text('previous release\n')
        with self.assertRaisesRegex(ValueError,'Existing release'):release.write_checksums(self.root,{'new.zip':'new'})
        self.assertEqual((self.root/'SHA256SUMS').read_text(),'previous release\n')

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


class NativeFeedVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'proofs').mkdir()
        for target, suffix in release.TARGETS.items():
            name = 'ibfetch-1.7.0-' + target + suffix
            p = self.root/name; p.write_bytes(('synthetic-'+target).encode())
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            p.with_name(name+'.sha256').write_text(digest+'  '+name+'\n')
            if target.startswith('macos-'):
                (self.root/'proofs'/(target+'.json')).write_text(json.dumps(dict(developer_id=True,
                    hardened_runtime=True,notarization_ticket=True,gatekeeper=True,archive=name,sha256=digest)))

    def test_first_updater_release_requires_the_feed_and_its_verified_package_digests(self):
        with self.assertRaisesRegex(ValueError, 'metadata'):
            release.verify_release(self.root, '1.7.0')
        release.prepare_update_feed(self.root, '1.7.0')
        checks = release.verify_release(self.root, '1.7.0')
        self.assertEqual(len(checks), 6)
        self.assertNotIn(b'\r', (self.root/'latest-mac.yml').read_bytes(), 'Feed bytes and their hash must be identical on Windows/Unix')
        feed = json.loads((self.root/'latest-mac.yml').read_text())
        self.assertEqual(feed['version'], '1.7.0')
        self.assertEqual([f['url'] for f in feed['files']], ['ibfetch-1.7.0-macos-arm64.zip','ibfetch-1.7.0-macos-x64.zip'])
        import base64
        for entry in feed['files']:
            payload = (self.root/entry['url']).read_bytes()
            self.assertEqual(entry['size'], len(payload))
            self.assertEqual(entry['sha512'], base64.b64encode(hashlib.sha512(payload).digest()).decode())
        release.write_checksums(self.root, checks)
        self.assertIn('latest-mac.yml', (self.root/'SHA256SUMS').read_text())

    def test_external_traversal_or_mismatched_feed_entries_never_pass_even_with_a_fresh_sidecar(self):
        release.prepare_update_feed(self.root, '1.7.0')
        path = self.root/'latest-mac.yml'; good = json.loads(path.read_text())
        for change in ({'url':'https://evil.invalid/update.zip'}, {'url':'../update.zip'}, {'sha512':'wrong'}, {'size':0}):
            with self.subTest(change=change):
                altered = {**good, 'files': [{**good['files'][0], **change},good['files'][1]]}
                path.write_text(json.dumps(altered))
                path.with_name(path.name+'.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name+'\n')
                with self.assertRaisesRegex(ValueError, 'metadata'):
                    release.verify_release(self.root, '1.7.0')

    def test_no_feed_is_created_before_both_apple_proofs_pass(self):
        proof = self.root/'proofs/macos-x64.json'
        data = json.loads(proof.read_text());data['notarization_ticket'] = False;proof.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'Apple verification'):
            release.prepare_update_feed(self.root, '1.7.0')
        self.assertFalse((self.root/'latest-mac.yml').exists())

    def test_conflicting_feed_is_preserved_instead_of_replaced(self):
        path = self.root/'latest-mac.yml'; path.write_text('previous candidate\n')
        with self.assertRaisesRegex(ValueError, 'Existing update'):
            release.prepare_update_feed(self.root, '1.7.0')
        self.assertEqual(path.read_text(), 'previous candidate\n')

"""Collect the locked production updater dependencies' supplied notices for desktop builds."""
import json
from pathlib import Path
import re
import shutil


def collect_updater_licenses(root, destination):
    lock = json.loads((root / 'electron/package-lock.json').read_text())
    modules = (root / 'electron/node_modules').resolve()
    if destination.is_symlink():
        raise ValueError('The generated npm license folder must not be a symbolic link.')
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    collected = []
    for relative, entry in lock['packages'].items():
        if not relative.startswith('node_modules/') or entry.get('dev') is True:
            continue
        source = (root / 'electron' / relative).resolve()
        source.relative_to(modules)
        metadata = json.loads((source / 'package.json').read_text())
        name, version = metadata['name'], metadata['version']
        if not re.fullmatch(r'(?:@[a-z0-9._-]+/)?[a-z0-9._-]+', name) or not re.fullmatch(r'[A-Za-z0-9.+_-]{1,100}', version):
            raise ValueError('Invalid locked npm package identity.')
        folder = destination / (name.replace('/', '__') + '-' + version)
        folder.mkdir(exist_ok=True)
        notices = [p for p in source.iterdir() if p.is_file() and p.name.lower().startswith(('license', 'licence', 'copying', 'notice'))]
        if not notices:
            # This exact package ships only its MIT declaration in package.json.
            # Retain it unchanged and include the standard permission/disclaimer;
            # never invent an upstream copyright line.
            if (name, version, metadata.get('license')) != ('lazy-val', '1.0.5', 'MIT'):
                raise ValueError('Missing supplied npm license: ' + name)
            shutil.copy2(source / 'package.json', folder / 'package.json')
            terms = (root / 'LICENSE').read_text().split('Permission is hereby granted', 1)[1]
            (folder / 'LICENSE.txt').write_text('lazy-val 1.0.5 declares MIT in its supplied package.json.\n'
                'No separate copyright notice is distributed by the package.\n\n'
                'Permission is hereby granted' + terms, encoding='utf-8')
        for notice in notices:
            if notice.is_symlink() or notice.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('Invalid npm license notice: ' + name)
            shutil.copy2(notice, folder / notice.name)
        collected.append({'name': name, 'version': version, 'license': metadata.get('license', '')})
    (destination / 'packages.json').write_text(json.dumps(collected, indent=2) + '\n', encoding='utf-8')
    return collected

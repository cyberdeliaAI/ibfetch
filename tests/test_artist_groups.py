"""Whole artist groups use the same boundaries as the sidebar, without wildcard ambiguity."""
from pathlib import Path
import tempfile
import unittest

import ibfetch as app


def library(names):
    return app.Library({
        'artists': {str(i): {'name': name} for i, name in enumerate(names, 1)},
        'albums': {str(i): {'name': 'Album', 'artist_id': i, 'tracks': [i]} for i in range(1, len(names)+1)},
        'tracks': {str(i): {'album_id': i, 'artist_id': i, 'genre': 'Jazz' if i % 2 else 'Rock'}
                   for i in range(1, len(names)+1)}}, 'synthetic-account', True)


class ArtistGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = app.App(Path(self.tmp.name))
        self.addCleanup(self.state.shutdown)
        self.state.lib = library(['10cc', '(Moods)', '# Hash', 'Élan', 'The 2 Bears', 'Zulu', '日本', 'ß Band'])

    def selected(self, **body):
        return {a['id'] for a in self.state.select(body)['albums']}

    def test_numbers_punctuation_non_latin_and_multi_character_capitals_are_in_hash_group(self):
        self.assertEqual(self.selected(artist_initial='#'), {1, 2, 3, 7, 8})
        self.assertEqual(self.selected(artist_initial='#', skip_the=True), {1, 2, 3, 5, 7, 8})
        self.assertEqual(self.selected(artist_initial='E'), {4})
        self.assertEqual(self.selected(artist_initial='T'), {5})

    def test_group_intersects_other_filters_and_literal_hash_search_is_preserved(self):
        self.assertEqual(self.selected(artist_initial='#', genre='Jazz'), {1, 3, 7})
        self.assertEqual(self.selected(artist='#*'), {3})
        self.assertEqual(self.selected(artist_initial='#', artist='10cc'), {1})

    def test_group_selection_is_not_limited_by_the_500_artist_sidebar_cap(self):
        self.state.lib = library([str(i) + ' Artist' for i in range(605)] + ['Alphabetic'])
        self.assertEqual(self.selected(artist_initial='#'), set(range(1, 606)))

    def test_invalid_group_is_rejected_instead_of_silently_selecting_everything(self):
        for value in ('All', 'AB', '', 1, True, [], {}):
            with self.subTest(value=value), self.assertRaises(app.Fail):
                self.state.select({'artist_initial': value})

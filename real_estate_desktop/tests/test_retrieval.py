import unittest

from real_estate_desktop.retrieval import BM25Index, rrf_fuse, tokenize


class Tokenizer(unittest.TestCase):
    def test_chinese_is_segmented_and_keeps_bigrams(self):
        tokens = tokenize('离地铁站多远')
        self.assertIn('地铁', tokens)
        self.assertIn('地铁站', tokens)  # bigram keeps phrase specificity

    def test_ascii_words_survive(self):
        self.assertIn('p013', tokenize('P013 的配套'))


class Lexical(unittest.TestCase):
    def setUp(self):
        self.index = BM25Index()
        self.index.build([
            ('a', '小区东侧有地铁站，步行约四百米。'),
            ('b', '周边有小学和幼儿园，招生范围需核实。'),
            ('c', '南门有便利店和生鲜超市，买菜方便。'),
        ])

    def test_relevant_chunk_ranks_first(self):
        hits = self.index.search('地铁 步行', limit=3)
        self.assertEqual(hits[0][0], 'a')

    def test_allowed_filter_limits_the_scope(self):
        hits = self.index.search('地铁', limit=3, allowed={'b', 'c'})
        self.assertEqual(hits, [])

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.index.search('   ', limit=3), [])


class Fusion(unittest.TestCase):
    def test_document_found_by_both_retrievers_wins(self):
        dense = [('b', .9), ('a', .8), ('c', .7)]
        lexical = [('a', 12.0), ('c', 9.0)]
        fused = rrf_fuse([dense, lexical], k=60)
        self.assertEqual(fused[0][0], 'a')  # rank 2 dense + rank 1 lexical

    def test_weights_shift_the_ranking(self):
        dense = [('a', .9), ('b', .8)]
        lexical = [('b', 5.0), ('a', 1.0)]
        fused = dict(rrf_fuse([dense, lexical], k=10, weights=[1.0, 3.0]))
        self.assertGreater(fused['b'], fused['a'])

    def test_invalid_k_is_rejected(self):
        with self.assertRaises(ValueError):
            rrf_fuse([[('a', 1.0)]], k=0)


if __name__ == '__main__':
    unittest.main()

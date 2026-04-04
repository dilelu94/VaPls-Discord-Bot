import unittest
from keywords import check_keywords

class TestKeywords(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(check_keywords("yo necesito pito"))
        self.assertTrue(check_keywords("necesito mucho pito"))
        self.assertTrue(check_keywords("pito es lo que necesito"))

    def test_no_detection(self):
        self.assertFalse(check_keywords("hola mundo"))
        self.assertFalse(check_keywords("tengo hambre"))
        self.assertFalse(check_keywords("no hay nada"))

if __name__ == '__main__':
    unittest.main()

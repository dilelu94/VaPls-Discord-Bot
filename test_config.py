import unittest
from unittest.mock import patch
import os

class TestConfig(unittest.TestCase):
    @patch.dict(os.environ, {"TOKEN": "test_token", "MODEL_PATH": "test_model", "AUDIO_DIR": "test_audio"})
    def test_config_loading(self):
        # We need to reload the config module to reflect changes in os.environ if it's already imported
        import importlib
        import config
        importlib.reload(config)
        
        self.assertEqual(config.TOKEN, "test_token")
        self.assertEqual(config.MODEL_PATH, "test_model")
        self.assertEqual(config.AUDIO_DIR, "test_audio")

if __name__ == '__main__':
    unittest.main()

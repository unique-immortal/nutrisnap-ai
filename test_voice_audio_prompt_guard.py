import unittest

import app as nutrisnap_app


class VoiceAudioPromptGuardTests(unittest.TestCase):
    def test_direct_audio_prompt_does_not_include_food_examples_that_can_leak_into_results(self):
        prompt = nutrisnap_app.build_voice_audio_direct_prompt()

        self.assertIn("one short food or drink phrase", prompt)
        self.assertIn("Never invent likely side dishes", prompt)
        self.assertIn("empty foods/exercises arrays", prompt)

        for leaked_example_token in ("鸭腿", "小翅根", "西瓜汁", "快走40分钟"):
            self.assertNotIn(leaked_example_token, prompt)


if __name__ == "__main__":
    unittest.main()

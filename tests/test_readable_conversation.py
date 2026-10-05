import copy
import unittest

from editorial_master import readable_conversation, conversation_markdown


def master(items):
    return {"project": {"name": "test"}, "media": {"duration": 100},
            "speakers": {"s1": {"label": "Hablante 1"}, "s2": {"label": "Hablante 2"}},
            "tracks": {"A": {"label": "Voz", "diarization": {"model": "test"}}},
            "conversation": {"utterances": items,
                             "clean_utterance_ids": [item["utterance_id"] for item in items]}}


def item(number, start, end, speaker="s1", text="Hola", **extra):
    return dict(utterance_id=f"A-u-{number:06d}", track_id="A", speaker_id=speaker,
                t_ini=start, t_fin=end, text=text, **extra)


class ReadableTests(unittest.TestCase):
    def test_groups_fragments_preserving_all_text_ids_times_and_master(self):
        source = master([item(1, 0, 1, text="¿Cómo"), item(2, 1.2, 2, text="estás?"),
                         item(3, 2.2, 3, "s2", "Bien.")])
        original = copy.deepcopy(source)
        blocks = readable_conversation(source)["blocks"]
        self.assertEqual([b["text"] for b in blocks], ["¿Cómo estás?", "Bien."])
        self.assertEqual(blocks[0]["utterance_ids"], ["A-u-000001", "A-u-000002"])
        self.assertEqual((blocks[0]["t_ini"], blocks[0]["t_fin"]), (0, 2))
        self.assertEqual(source, original)
        output = conversation_markdown(source)
        self.assertIn("Hablante 2", output)
        self.assertIn("`A-u-000001` → `A-u-000002`", output)

    def test_interruptions_pauses_and_unknown_speakers_stay_separate(self):
        source = master([item(1, 0, 1), item(2, 1, 2, "s2"), item(3, 2, 3),
                         item(4, 7, 8), item(5, 8, 9, None), item(6, 9, 10, None)])
        self.assertEqual(len(readable_conversation(source)["blocks"]), 6)

    def test_overlaps_are_not_collapsed_and_duplicate_filter_is_respected(self):
        source = master([item(1, 0, 2, overlap_group="overlap-1"), item(2, 1, 3), item(3, 3, 4)])
        source["conversation"]["clean_utterance_ids"] = ["A-u-000001", "A-u-000002"]
        blocks = readable_conversation(source)["blocks"]
        self.assertEqual(len(blocks), 2)
        self.assertIn("habla superpuesta", conversation_markdown(source))
        self.assertEqual([i for b in blocks for i in b["utterance_ids"]], source["conversation"]["clean_utterance_ids"])

    def test_long_monologues_have_bounded_blocks(self):
        source = master([item(n, n*10, n*10+9) for n in range(8)])
        blocks = readable_conversation(source)["blocks"]
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(b["t_fin"] - b["t_ini"] <= 60 for b in blocks))

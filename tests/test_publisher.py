import unittest

from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo

from bot.publisher import build_input_media


class BuildInputMediaTests(unittest.TestCase):
    def test_maps_types_and_captions_only_first_item(self) -> None:
        items = [
            {"type": "photo", "file_id": "photo1"},
            {"type": "video", "file_id": "video1"},
            {"type": "photo", "file_id": "photo2"},
        ]

        media = build_input_media(items, caption="подпись альбома")

        self.assertIsInstance(media[0], InputMediaPhoto)
        self.assertEqual(media[0].media, "photo1")
        self.assertEqual(media[0].caption, "подпись альбома")

        self.assertIsInstance(media[1], InputMediaVideo)
        self.assertIsNone(media[1].caption)

        self.assertIsInstance(media[2], InputMediaPhoto)
        self.assertIsNone(media[2].caption)

    def test_unknown_type_falls_back_to_photo(self) -> None:
        media = build_input_media([{"type": "sticker", "file_id": "abc"}])

        self.assertIsInstance(media[0], InputMediaPhoto)

    def test_document_type_maps_correctly(self) -> None:
        media = build_input_media([{"type": "document", "file_id": "doc1"}])

        self.assertIsInstance(media[0], InputMediaDocument)


if __name__ == "__main__":
    unittest.main()

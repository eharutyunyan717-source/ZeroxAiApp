import unittest

from telegram_bot import (
    build_slot_symbols,
    format_duration,
    format_shop_item_block,
    parse_transfer_input,
)


class ShopFormattingTests(unittest.TestCase):
    def test_format_duration_for_days(self):
        self.assertEqual(format_duration(168), "7 дней")
        self.assertEqual(format_duration(24), "1 день")

    def test_format_shop_item_block_includes_price_and_duration(self):
        item = {
            "name": "💎 VIP статус",
            "price": 100000,
            "duration_hours": 168,
            "description": "Показывает значок рядом с балансом",
            "badge": "💎",
        }
        block = format_shop_item_block("vip_status", item, "Доступно")
        self.assertIn("💎 VIP статус", block)
        self.assertIn("100,000", block)
        self.assertIn("7 дней", block)
        self.assertIn("Доступно", block)

    def test_parse_transfer_input_rejects_single_huge_numeric_argument(self):
        target_ref, amount = parse_transfer_input(["60000000000000000"], {"reply_to_message": None, "entities": []})
        self.assertIsNone(target_ref)
        self.assertIsNone(amount)

    def test_parse_transfer_input_uses_reply_target(self):
        target_ref, amount = parse_transfer_input(["100"], {"reply_to_message": {"from": {"id": 42}}, "entities": []})
        self.assertEqual(target_ref, 42)
        self.assertEqual(amount, 100)

    def test_build_slot_symbols_for_jackpot_luck(self):
        symbols = build_slot_symbols(25, 5, ("■", "🍇", "🍋"))
        self.assertEqual(symbols[0], symbols[1])
        self.assertEqual(symbols[1], symbols[2])

    def test_build_slot_symbols_for_pair_luck(self):
        symbols = build_slot_symbols(25, 30, ("■", "🍇", "🍋"))
        self.assertEqual(symbols[0], symbols[1])
        self.assertNotEqual(symbols[1], symbols[2])


if __name__ == "__main__":
    unittest.main()

from pathlib import Path

path = Path(r"C:\Users\erikh\Documents\New project\ZeroxAiApp\telegram_bot.py")
text = path.read_text(encoding='utf-8')
start = text.index('        if cmd == "/shop":')
end = text.index('        if cmd == "/ben":', start)
new_block = '''        if cmd == "/shop":
            if not args:
                lines = ["🏪 <b>Магазин привилегий</b>", "Используйте <code>/shop buy &lt;id&gt;</code> для покупки.", ""]
                for item_id, item in SHOP_ITEMS.items():
                    lines.append(format_shop_item_block(item_id, item, get_shop_status_text(user_id, item_id)))
                    lines.append("")
                reply("\n".join(lines), "HTML")
                return True

            if args[0].lower() == "buy" and len(args) > 1:
                item_id = args[1].lower()
                item = SHOP_ITEMS.get(item_id)
                if not item:
                    reply("❌ Такого предмета нет в магазине.")
                    return True

                balance = get_balance(user_id)
                if balance < item['price']:
                    reply(f"❌ Недостаточно монет. Нужно {fmt_coin(item['price'])}, у вас {fmt_coin(balance)}.")
                    return True

                if has_active_item(user_id, item_id):
                    reply(f"❌ У вас уже активен этот предмет: <b>{item['name']}</b>.", "HTML")
                    return True

                add_balance(user_id, -item['price'])
                user_items = get_user_items(user_id)
                user_items[item_id] = {
                    "purchased_at": time.time(),
                    "expires_at": time.time() + item['duration_hours'] * 3600
                }
                set_user_items(user_id, user_items)
                reply(f"✅ Вы успешно купили <b>{item['name']}</b>!\nСрок действия: {format_duration(item['duration_hours'])}.\nБаланс: {fmt_coin(get_balance(user_id))}.", "HTML")
                return True
            return True

'''
path.write_text(text[:start] + new_block + text[end:], encoding='utf-8')
print('patched shop block')

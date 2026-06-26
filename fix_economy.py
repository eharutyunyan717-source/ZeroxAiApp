from pathlib import Path

path = Path(r"C:\Users\erikh\Documents\New project\ZeroxAiApp\telegram_bot.py")
text = path.read_text(encoding='utf-8')
start = text.index('        if cmd == "/free":')
end = text.index('        if cmd in ("/bal", "/balance"):', start)
new_block = '''        if cmd == "/free":
            can_claim, message = can_claim_free(user_id)
            if not can_claim:
                reply(f"⏳ {message}")
                return True
            add_balance(user_id, STARTING_BALANCE)
            set_claim(user_id, "free")
            reply(f"💰 Вы получили {STARTING_BALANCE} монет! Ваш баланс: {get_balance(user_id)}.")
            return True

        if cmd == "/promo":
            if not args:
                reply("Использование: /promo Aibot2026")
                return True
            ok, result = redeem_promo(user_id, args[0])
            if not ok:
                reply(f"⚠️ {result}")
                return True
            reply(f"🎉 Промокод активирован! Вы получили {result} монет. Баланс: {get_balance(user_id)}")
            return True

'''
path.write_text(text[:start] + new_block + text[end:], encoding='utf-8')
print('patched')

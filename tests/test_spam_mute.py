import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import telegram_bot as tb


def test_spam_threshold_triggers_mute(monkeypatch):
    monkeypatch.setattr(tb, "get_chat_data", lambda chat_id: {"muted": {}})
    monkeypatch.setattr(tb, "save_data", lambda: None)
    monkeypatch.setattr(tb, "telegram_request", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(tb, "reply_message", lambda *args, **kwargs: None)

    chat_id = 123
    user_id = 456
    for i in range(4):
        assert tb.check_spam_and_mute(None, chat_id, user_id, message_id=100 + i, now=1000.0 + i) is False

    assert tb.check_spam_and_mute(None, chat_id, user_id, message_id=104, now=1004.0) is True

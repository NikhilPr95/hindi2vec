from alerter.notify import Notifier


def test_channels_detected_from_env():
    assert Notifier(env={}).channels == []
    assert Notifier(env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}).channels == ["telegram"]
    assert Notifier(env={"NTFY_TOPIC": "x"}).channels == ["ntfy"]
    env = {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p", "EMAIL_TO": "me@x"}
    assert Notifier(env=env).channels == ["email"]


def test_dry_run_sends_nothing(capsys):
    n = Notifier(env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, dry_run=True)
    assert n.send("hello") == []
    assert "hello" in capsys.readouterr().out


def test_one_broken_channel_does_not_block_others(monkeypatch):
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "NTFY_TOPIC": "x"}
    n = Notifier(env=env)
    sent = []
    monkeypatch.setattr(Notifier, "_send_telegram", lambda self, t, s: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(Notifier, "_send_ntfy", lambda self, t, s: sent.append(t))
    errs = n.send("hi")
    assert errs == ["telegram: boom"]
    assert sent == ["hi"]

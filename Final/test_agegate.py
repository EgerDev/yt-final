import yt_final
YD = yt_final.YouTubeDownloader


def test_classify_agegate_before_bot():
    assert YD._classify_error("Sign in to confirm your age. This video may be inappropriate for some users") == "agegate"
    assert YD._classify_error("This video is age-restricted") == "agegate"
    # a genuine bot/throttle message is still 'bot', not agegate
    assert YD._classify_error("Sign in to confirm you're not a bot") == "bot"


def test_is_auth_gate():
    assert YD._is_auth_gate("agegate") is True
    assert YD._is_auth_gate("bot") is True
    assert YD._is_auth_gate("format") is False
    assert YD._is_auth_gate(None) is False


def test_agegate_message_names_auth_commands():
    msg = YD._download_error_message(
        Exception("Sign in to confirm your age. inappropriate for some users"), None, False)
    assert "auth login" in msg and "auth import" in msg

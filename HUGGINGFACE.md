# ZeroxAI on HuggingFace Spaces

Use these files for a Gradio Space:

- `app.py`
- `requirements.txt`
- `assets/logo.svg`

In HuggingFace Space settings, add a secret:

```text
ZEROXAI_API_KEYS
```

Value example:

```text
gsk_first_key,gsk_second_key,gsk_third_key
```

Do not commit real API keys into the repository. The app uses `openai/gpt-oss-120b` through Groq's OpenAI-compatible endpoint and automatically tries the next key if one fails with a retryable error.

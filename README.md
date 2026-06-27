# ZeroxAI

ZeroxAI is a production-ready cross-platform AI chat application built as an installable PWA. It runs on mobile browsers, tablets, desktop browsers, and can be wrapped for App Store, Google Play, and Windows distribution with Capacitor or a WebView shell.

## Features

- ChatGPT-style adaptive interface for phones, tablets, and desktop.
- Persistent chat history in local storage.
- Configurable OpenAI-compatible API provider, base URL, model, and API key.
- API key encrypted locally with Web Crypto AES-GCM and a user vault passphrase.
- Light, dark, and system theme modes.
- Copy buttons for AI responses.
- Animated send/receive states.
- Offline shell caching with a service worker.
- ZeroxAI brand logo and installable PWA manifest.
- Server-side ZeroxAI behavior prompt for better Russian conversation and coding help.

## Run

Open `index.html` directly for quick inspection. For the full PWA experience, serve the folder over localhost:

```bash
npm run dev
```

Then open:

```text
http://localhost:5173
```

## Build

Create a production-ready static PWA bundle:

```bash
npm run build
```

The output is written to `dist/`. Keep API keys on the server or hosting platform; do not copy `.env` into a public static bundle.

## API Setup

ZeroxAI uses the server-side endpoint `/api/chat`, so API keys are not exposed in the browser. The server calls Groq's OpenAI-compatible API with the fixed model:

```text
openai/gpt-oss-120b
```

Create a local `.env` file and add your keys as a comma-separated list:

```text
ZEROXAI_API_KEYS=gsk_first_key,gsk_second_key,gsk_third_key
```

If one key reaches a limit or fails with a retryable provider error, the server automatically tries the next key.

## Model Behavior

The app does not train provider model weights locally. Instead, `scripts/dev-server.js` injects a ZeroxAI system prompt before each chat request. This makes the assistant:

- answer in clear Russian when the user writes in Russian;
- avoid mocking typos and infer the user's intent;
- write cleaner programming answers;
- explain errors in simple language;
- avoid exposing API keys in client-side code.

## Publishing Path

For HuggingFace Spaces, use the included Gradio app:

```bash
python app.py
```

Upload `app.py`, `requirements.txt`, and `assets/logo.svg` to a Gradio Space. Add `ZEROXAI_API_KEYS` in Space secrets. More details are in `HUGGINGFACE.md`.

For Telegram, use the included bot:

```bash
python telegram_bot.py
```

Set `TELEGRAM_BOT_TOKEN` and `ZEROXAI_API_KEYS` before launch. More details are in `TELEGRAM.md`.

For App Store and Google Play, wrap this PWA with Capacitor and use native secure storage if your compliance requirements demand OS-level keychain storage:

```bash
npm create @capacitor/app
npx cap add ios
npx cap add android
npx cap sync
```

For Windows, publish as a PWA through Microsoft Edge or package the same web app with a native WebView wrapper.

## Project Structure

```text
assets/               ZeroxAI logo and app icons
src/app.js            Application state, API, encryption, chat logic
src/styles.css        Responsive UI, themes, animations
index.html            App shell
manifest.webmanifest  PWA manifest
service-worker.js     Offline shell cache
scripts/check-assets.js
```

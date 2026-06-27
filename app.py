import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import gradio as gr


MODEL = "openai/gpt-oss-120b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
REQUEST_TIMEOUT = 90

ACTIVE_KEY_INDEX = 0

SYSTEM_PROMPT = """
Ты ZeroxAI - умный, спокойный и полезный AI-ассистент.

Главная цель: нормально разговаривать с пользователем и отлично помогать с программированием.

Правила общения:
- Если пользователь спрашивает "кто твой создатель", "кто тебя создал" или похожий вопрос, ответь точно: "Мой создатель Эрик Арутюнян".
- Отвечай на языке пользователя. Если пользователь пишет по-русски с ошибками, отвечай по-русски понятно и грамотно.
- Пиши просто, по делу и без лишней воды.
- Не высмеивай ошибки пользователя. Мягко понимай смысл и помогай.
- Если запрос неясный, сначала сделай разумное предположение. Задавай вопрос только если без ответа нельзя продолжить.
- Для обычных вопросов давай короткий полезный ответ.

Правила программирования:
- Пиши рабочий, чистый и понятный код.
- Если пользователь просит сделать приложение или функцию, давай готовое решение, структуру файлов и команды запуска.
- Объясняй ошибки простым языком и показывай, как исправить.
- Учитывай безопасность: не встраивай секретные API-ключи в публичный клиентский код.
- Для больших задач разбивай ответ на шаги.
- Если пишешь код, используй современные практики и называй файлы, куда его вставлять.

Стиль:
- Ты дружелюбный профессиональный помощник ZeroxAI.
- Не притворяйся, что обучаешь собственные веса модели. Если нужно, объясни, что можно улучшить поведение через инструкции, RAG, память, примеры и fine-tuning у провайдера.
""".strip()


CSS = """
:root {
  --zerox-bg: #f4fbff;
  --zerox-panel: rgba(255,255,255,.9);
  --zerox-border: rgba(14,165,233,.22);
  --zerox-text: #0f2742;
  --zerox-muted: #5e7894;
  --zerox-blue: #38bdf8;
  --zerox-blue-strong: #0ea5e9;
}

body, .gradio-container {
  color: var(--zerox-text) !important;
  background:
    linear-gradient(115deg, rgba(255,255,255,.8) 0 18%, transparent 18% 36%, rgba(224,247,255,.62) 36% 54%, transparent 54% 72%, rgba(186,230,253,.42) 72% 100%),
    linear-gradient(135deg, #ffffff 0%, var(--zerox-bg) 52%, #e8f8ff 100%) !important;
  background-size: 220% 220%, 100% 100% !important;
  animation: skyFlow 18s ease-in-out infinite !important;
}

.zerox-hero {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px;
  margin-bottom: 12px;
  border: 1px solid var(--zerox-border);
  border-radius: 14px;
  background: var(--zerox-panel);
  box-shadow: 0 20px 70px rgba(56,189,248,.18);
  animation: rise .55s cubic-bezier(.2,.9,.2,1) both;
}

.zerox-logo {
  display: grid;
  place-items: center;
  width: 58px;
  height: 58px;
  flex: 0 0 auto;
  border-radius: 16px;
  color: #05233a;
  font-size: 30px;
  font-weight: 900;
  background: linear-gradient(135deg, #f8fdff, #7dd3fc, #0ea5e9);
  box-shadow: 0 14px 38px rgba(56,189,248,.36);
  animation: floatLogo 4s ease-in-out infinite, glowLogo 2.6s ease-in-out infinite;
}

.zerox-hero h1 {
  margin: 0;
  font-size: 30px;
  line-height: 1;
  background: linear-gradient(90deg, #0f2742, #0284c7, #0f2742);
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: titleSheen 4.5s ease-in-out infinite;
}

.zerox-hero p {
  margin: 6px 0 0;
  color: var(--zerox-muted);
}

.contain, .block, .panel {
  border-color: var(--zerox-border) !important;
  border-radius: 14px !important;
  box-shadow: 0 14px 48px rgba(56,189,248,.12) !important;
}

button.primary, button.secondary, .lg {
  transition: transform .18s ease, box-shadow .18s ease !important;
}

button:hover {
  transform: translateY(-2px);
  box-shadow: 0 16px 44px rgba(56,189,248,.22) !important;
}

textarea, input {
  border-color: var(--zerox-border) !important;
}

textarea:focus, input:focus {
  box-shadow: 0 0 0 3px rgba(56,189,248,.2) !important;
}

@keyframes skyFlow {
  0%, 100% { background-position: 0% 50%, 0 0; }
  50% { background-position: 100% 50%, 0 0; }
}

@keyframes rise {
  from { opacity: 0; transform: translateY(16px) scale(.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

@keyframes floatLogo {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-5px); }
}

@keyframes glowLogo {
  0%, 100% { filter: drop-shadow(0 0 0 rgba(56,189,248,0)); }
  50% { filter: drop-shadow(0 0 16px rgba(56,189,248,.6)); }
}

@keyframes titleSheen {
  0%, 100% { background-position: 0% 50%; }
  50% { background-position: 100% 50%; }
}
"""


def load_dotenv():
    try:
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except:
        pass

load_dotenv()

def get_api_keys():
    raw_keys = os.getenv("ZEROXAI_API_KEYS") or os.getenv("GROQ_API_KEYS") or ""
    return [key.strip() for key in re.split(r"[,|\n]+", raw_keys) if key.strip()]


def as_messages(history, user_message):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history[-24:]:
        if isinstance(item, dict):
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
            continue

        if isinstance(item, (list, tuple)) and len(item) >= 2:
            user_text, assistant_text = item[0], item[1]
            if user_text:
                messages.append({"role": "user", "content": str(user_text)})
            if assistant_text:
                messages.append({"role": "assistant", "content": str(assistant_text)})

    messages.append({"role": "user", "content": user_message})
    return messages


def call_groq(messages):
    global ACTIVE_KEY_INDEX

    api_keys = get_api_keys()
    if not api_keys:
        return "Ошибка: добавьте ZEROXAI_API_KEYS в HuggingFace Space Settings -> Secrets."

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.55,
        "top_p": 0.9,
    }

    last_error = "Все API-ключи не сработали."
    for attempt in range(len(api_keys)):
        key_index = (ACTIVE_KEY_INDEX + attempt) % len(api_keys)
        api_key = api_keys[key_index]
        request = urllib.request.Request(
            f"{GROQ_BASE_URL}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ZeroxAI-HuggingFace-Space/1.0",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
                ACTIVE_KEY_INDEX = key_index
                return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            last_error = extract_error_message(detail) or f"API вернул статус {error.code}"
            if "1010" in detail or "error code: 1010" in detail.lower():
                last_error = (
                    "Groq/Cloudflare заблокировал запрос с HuggingFace Space. "
                    "Проверьте, что ключи Groq активны, и попробуйте перезапустить Space. "
                    "Если ошибка останется, лучше подключить другой OpenAI-compatible endpoint."
                )
                break
            if error.code not in {401, 403, 408, 409, 429, 500, 502, 503, 504}:
                break
        except Exception as error:
            last_error = str(error)

        time.sleep(0.2)

    ACTIVE_KEY_INDEX = (ACTIVE_KEY_INDEX + 1) % len(api_keys)
    return f"Не удалось получить ответ: {last_error}"


def extract_error_message(raw):
    try:
        data = json.loads(raw)
        error = data.get("error")
        if isinstance(error, dict):
            return error.get("message")
        if isinstance(error, str):
            return error
    except Exception:
        return raw[:300]
    return raw[:300]


def chat(message, history):
    if not message.strip():
        return "", history
    history = normalize_history(history)
    answer = call_groq(as_messages(history, message.strip()))
    history.append({"role": "user", "content": message.strip()})
    history.append({"role": "assistant", "content": answer})
    return "", history


def normalize_history(history):
    normalized = []
    for item in history or []:
        if isinstance(item, dict):
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content is not None:
                normalized.append({"role": role, "content": str(content)})
            continue

        if isinstance(item, (list, tuple)) and len(item) >= 2:
            user_text, assistant_text = item[0], item[1]
            if user_text:
                normalized.append({"role": "user", "content": str(user_text)})
            if assistant_text:
                normalized.append({"role": "assistant", "content": str(assistant_text)})

    return normalized


THEME = gr.themes.Soft(
    primary_hue="sky",
    secondary_hue="blue",
    neutral_hue="slate",
    radius_size="lg",
)


with gr.Blocks(title="ZeroxAI") as demo:
    gr.HTML(
        """
        <div class="zerox-hero">
          <div class="zerox-logo">Z</div>
          <div>
            <h1>ZeroxAI</h1>
            <p>Бело-голубой AI-чат для разговоров, идей и программирования.</p>
          </div>
        </div>
        """
    )

    chatbot = gr.Chatbot(height=560)
    textbox = gr.Textbox(
        placeholder="Напишите сообщение ZeroxAI...",
        lines=2,
        max_lines=8,
        label="Сообщение",
    )
    with gr.Row():
        submit = gr.Button("Отправить", variant="primary")
        clear = gr.Button("Очистить")

    gr.Examples(
        examples=[
            "Кто твой создатель?",
            "Помоги написать Python-код для сайта",
            "Объясни простыми словами, что такое API",
        ],
        inputs=textbox,
    )

    textbox.submit(chat, [textbox, chatbot], [textbox, chatbot])
    submit.click(chat, [textbox, chatbot], [textbox, chatbot])
    clear.click(lambda: [], None, chatbot, queue=False)


if __name__ == "__main__":
    import threading
    import telegram_bot

    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        threading.Thread(target=telegram_bot.main, daemon=True).start()

    port = int(os.getenv("PORT", "7860"))
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        css=CSS,
        theme=THEME,
    )

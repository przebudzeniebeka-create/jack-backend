# check_env.py
import os
from dotenv import load_dotenv, find_dotenv

dotenv_path = find_dotenv(usecwd=True)
if not dotenv_path:
    print("❌ Nie znalazłem .env w bieżącym folderze. Upewnij się, że plik .env leży obok tego skryptu.")
else:
    print(f"ℹ️ Ładuję .env z: {dotenv_path}")
    load_dotenv(dotenv_path)

required = [
    "OPENAI_API_KEY", "OPENAI_MODEL",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID_EN", "ELEVENLABS_VOICE_ID_PL", "ELEVENLABS_MODEL_ID",
    "TURNSTILE_SECRET_KEY", "DATABASE_URL", "PORT"
]

missing = [k for k in required if not os.getenv(k)]

if missing:
    print("❌ Brak zmiennych:", ", ".join(missing))
else:
    print("✅ OK — wszystkie wymagane zmienne są dostępne.")

# Pokaż tylko bezpieczne, niesekretne rzeczy:
print("OPENAI_MODEL:", os.getenv("OPENAI_MODEL"))
print("PORT:", os.getenv("PORT"))

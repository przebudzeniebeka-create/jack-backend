def build_conversation_history(conversation):
    history = []
    for msg in conversation:
        history.append({"role": "user", "content": msg["user"]})
        history.append({"role": "assistant", "content": msg["assistant"]})
    return history

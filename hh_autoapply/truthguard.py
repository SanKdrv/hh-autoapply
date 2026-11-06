"""Страж фактов: не даём модели приписать кандидату незнакомые технологии.

Профиль кандидата — единственный источник правды. Если в письме или ответе
появляется технология, которой в профиле нет, считаем это выдумкой:
- из письма такие предложения вырезаем;
- ответ в чате с такой фразой не отправляем, а эскалируем человеку.
"""
from __future__ import annotations

import json
import re

# Технологии, которые часто встречаются в вакансиях. Всё, чего нет в профиле
# кандидата, считается «чужим» и упоминать это как свой опыт нельзя.
TECH_VOCAB = (
    # AI / LLM
    "langgraph", "langchain", "llamaindex", "crewai", "autogen", "qdrant", "chroma",
    "pinecone", "weaviate", "milvus", "faiss", "pgvector", "mcp", "ollama", "openai",
    "anthropic", "claude", "gemini", "huggingface", "transformers", "vllm", "ray",
    "tensorrt", "triton", "mlflow", "kubeflow", "dspy", "ragas", "gpt", "llm",
    # AI-инструменты разработки
    "cursor", "codex", "copilot", "chatgpt", "windsurf", "aider", "continue.dev",
    "opencode", "replit", "bolt.new", "lovable", "v0.dev", "devin", "cline", "roo code",
    # данные / инфраструктура
    "kafka", "rabbitmq", "kubernetes", "k8s", "terraform", "ansible", "jenkins",
    "argocd", "helm", "airflow", "spark", "hadoop", "mongodb", "mysql", "mssql",
    "elasticsearch", "opensearch", "cassandra", "dynamodb", "minio", "ceph",
    "prometheus", "grafana", "victoriametrics", "loki", "jaeger", "zipkin",
    "nats", "redis", "postgres", "postgresql", "clickhouse", "oracle", "sqlite",
    "docker", "nginx", "traefik", "grpc", "protobuf", "graphql", "websocket",
    # языки / фреймворки
    "django", "flask", "fastapi", "react", "angular", "svelte", "nextjs", "nuxt",
    "vue", "nestjs", "express", "node", "typescript", "javascript", "php", "laravel",
    "symfony", "yii", "bitrix", "wordpress", "woocommerce", "java", "kotlin", "swift",
    "rust", "golang", "python", "scala", "clojure", "erlang", "dotnet", "csharp",
)

# Слова-заявки: именно с ними нельзя упоминать незнакомую технологию.
CLAIM_WORDS = (
    "работал", "работаю", "использ", "владею", "есть опыт", "опыт с",
    "опыт работы", "знаком", "изуча", "близк", "применял", "применяю",
    "писал на", "делал на", "строил на", "разрабатывал на",
)

NEG_RE = re.compile(r"(?<![а-яёa-z])(не|нет|без)(?![а-яёa-z])")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#/_\-]{1,}")
SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")


def profile_blob(profile: dict, extra: str = "") -> str:
    """Весь профиль одной строкой в нижнем регистре — источник правды."""
    try:
        blob = json.dumps(profile, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(profile)
    return (blob + " " + extra).lower()


def _known(token: str, blob: str) -> bool:
    t = token.lower().strip(".,;:()[]«»\"'")
    if not t:
        return True
    # сравнение по границам, чтобы «go» не считалось частью «django»
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", blob) is not None


def foreign_techs(text: str, profile: dict, vac: dict | None = None) -> list[str]:
    """Технологии из текста, которых нет в профиле (по словарю TECH_VOCAB)."""
    vac = vac or {}
    company = f"{vac.get('company') or ''} {vac.get('title') or ''}"
    blob = profile_blob(profile, company)
    low = (text or "").lower()
    found: list[str] = []
    for tech in TECH_VOCAB:
        if tech in low and not _known(tech, blob) and tech not in found:
            found.append(tech)
    return found


def foreign_in_text(text: str, profile: dict, vac: dict | None = None) -> list[str]:
    """Чужие технологии, реально упомянутые в готовом тексте (любой регистр)."""
    vac = vac or {}
    blob = profile_blob(profile, f"{vac.get('company') or ''} {vac.get('title') or ''}")
    found: list[str] = []
    for token in TOKEN_RE.findall(text or ""):
        t = token.lower().strip(".,;:()[]«»\"'")
        if len(t) < 2 or t in found:
            continue
        if t in TECH_VOCAB and not _known(t, blob):
            found.append(t)
    return found


def impossible_block(text: str, profile: dict, vac: dict | None = None) -> str:
    """Блок для промпта: какие технологии из вакансии кандидату запрещено упоминать."""
    foreign = foreign_techs(text, profile, vac)
    if not foreign:
        return ""
    names = ", ".join(foreign)
    return (
        "\n## ЗАПРЕЩЁННЫЕ ТЕХНОЛОГИИ (у кандидата их НЕТ)\n"
        f"{names}\n"
        "Не упоминай их вообще: ни как опыт, ни как «близкую тему», ни как «изучаю».\n"
        "Если вакансия про них — говори только о своём релевантном опыте и о готовности\n"
        "освоить инструмент, без намёков, что уже работал с ним.\n"
    )


def _is_honest_disclaimer(sentence: str, tech: str = "") -> bool:
    """Честная оговорка — только если отрицание стоит рядом с технологией."""
    low = sentence.lower()
    idx = low.find(tech) if tech else 0
    window = low[max(0, idx - 80): idx + 200]
    if not NEG_RE.search(window):
        return False
    return any(w in window for w in (
        "работал", "использ", "опыт", "знаком", "применял", "писал", "делал",
        "сталкивал", "пробовал", "трогал",
    ))


def strip_unknown_claims(text: str, profile: dict,
                         vac: dict | None = None) -> tuple[str, list[str]]:
    """Вырезает из письма предложения с незнакомыми технологиями.

    Возвращает (очищенный текст, список вырезанных технологий).
    """
    foreign = set(foreign_in_text(text, profile, vac))
    if not foreign:
        return text, []

    kept_lines: list[str] = []
    removed: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            kept_lines.append(line)
            continue
        kept: list[str] = []
        for chunk in SENTENCE_RE.findall(line):
            low = chunk.lower()
            hit = next((t for t in foreign if re.search(
                rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low)), None)
            if hit and not _is_honest_disclaimer(chunk, hit):
                removed.append(hit)
                continue
            kept.append(chunk.strip())
        kept_lines.append(" ".join(k for k in kept if k))

    cleaned = "\n".join(kept_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, sorted(set(removed))


def claims_unknown(text: str, profile: dict, vac: dict | None = None) -> list[str]:
    """Для ответов в чате: список незнакомых технологий, о которых заявлен опыт."""
    foreign = foreign_in_text(text, profile, vac)
    if not foreign:
        return []
    bad: list[str] = []
    for chunk in SENTENCE_RE.findall(text):
        low = chunk.lower()
        hit = next((t for t in foreign if re.search(
            rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low)), None)
        if not hit:
            continue
        if _is_honest_disclaimer(chunk, hit):
            continue
        if any(w in low for w in CLAIM_WORDS):
            bad.append(hit)
    return sorted(set(bad))

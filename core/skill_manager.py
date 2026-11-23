"""2-Layer Composable Skill Hub Manager (skills/languages/*.md + skills/styles/*.md)."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ruamel.yaml import YAML

from core.atomic_io import atomic_write_text

APP_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = APP_DIR / "skills"
LANG_SKILLS_DIR = SKILLS_DIR / "languages"
STYLE_SKILLS_DIR = SKILLS_DIR / "styles"

LAYER_TOKEN_LIMITS = {
    "languages": 110,
    "styles": 160,
}

_yaml = YAML(typ="safe")
_yaml_dump = YAML()
_yaml_dump.default_flow_style = False
_yaml_dump.allow_unicode = True


def estimate_tokens(text: str) -> int:
    """Fast heuristic token estimator matching Qwen/Gemma subword density (~3.6 chars/tok)."""
    clean = (text or "").strip()
    if not clean:
        return 0
    return max(1, int(round(len(clean) / 3.6)))


def _slugify_id(raw_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", (raw_id or "").strip().lower()).strip("_")
    return slug[:48] or f"skill_{hashlib.md5((raw_id or 'x').encode()).hexdigest()[:6]}"


def _split_frontmatter_and_body(md_text: str) -> Tuple[Dict[str, Any], str]:
    normalized = (md_text or "").replace("\r\n", "\n").lstrip("\ufeff")
    if normalized.startswith("---\n"):
        end_idx = normalized.find("\n---\n", 4)
        if end_idx == -1 and normalized.endswith("\n---"):
            end_idx = len(normalized) - 4
        if end_idx != -1:
            fm_raw = normalized[4:end_idx]
            body = normalized[end_idx + 5 :].strip()
            try:
                meta = _yaml.load(fm_raw) or {}
                if isinstance(meta, dict):
                    return meta, body
            except Exception:
                pass
    return {}, normalized.strip()


def _parse_sections(body: str) -> Tuple[str, List[Dict[str, str]]]:
    """Extract Rules markdown block and 2-column Example pairs from a Skill body."""
    rules_lines: List[str] = []
    examples: List[Dict[str, str]] = []
    mode = "rules"

    for line in (body or "").splitlines():
        st = line.strip()
        low = st.lower()
        if low.startswith("### ví dụ") or low.startswith("### example"):
            mode = "examples"
            continue
        if low.startswith("### quy tắc") or low.startswith("### rule"):
            mode = "rules"
            continue
        if mode == "rules":
            if st:
                rules_lines.append(line.rstrip())
        else:
            if "->" in st:
                left, right = st.split("->", 1)
                left_c = re.sub(r"^\d+\|", "", left.strip()).strip()
                right_c = re.sub(r"^\d+\|", "", right.strip()).strip()
                if left_c or right_c:
                    examples.append({"source": left_c, "target": right_c})

    return "\n".join(rules_lines).strip(), examples


def _render_markdown(meta: Dict[str, Any], rules_text: str, examples: List[Dict[str, str]]) -> str:
    buf = io.StringIO()
    _yaml_dump.dump(meta, buf)
    fm_str = buf.getvalue().strip()

    r_lines = []
    for ln in (rules_text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        r_lines.append(s if s.startswith("-") else f"- {s}")
    if not r_lines:
        r_lines = ["- Dịch tự nhiên, chuẩn ngữ cảnh game và bảo toàn 100% thẻ __TAG_x__ / __NUM_x__."]

    ex_lines = []
    for idx, ex in enumerate(examples or [], start=1):
        src = (ex.get("source") or "").strip()
        tgt = (ex.get("target") or "").strip()
        if src and tgt:
            ex_lines.append(f"{idx}|{src} -> {idx}|{tgt}")

    parts = ["---", fm_str, "---", "### Quy tắc", "\n".join(r_lines)]
    if ex_lines:
        parts.extend(["", "### Ví dụ mẫu", "\n".join(ex_lines)])
    return "\n".join(parts).strip() + "\n"


def _lang_match_score(patterns: List[str], code: str) -> int:
    """Return 2 for exact code/prefix match, 1 for wildcard '*', 0 for no match."""
    c_low = (code or "").strip().lower().replace("_", "-")
    c_base = c_low.split("-")[0]
    best = 0
    for p in patterns or []:
        p_low = str(p or "").strip().lower().replace("_", "-")
        if not p_low:
            continue
        if p_low == "*":
            best = max(best, 1)
        elif p_low == c_low or (p_low.split("-")[0] == c_base and c_base != "zh"):
            best = max(best, 2)
        elif c_base == "zh" and p_low == c_base and c_low in ("zh", "zh-cn", "zh-hans"):
            best = max(best, 2)
    return best


class SkillManager:
    """Loads, validates token budgets, composes KV-prefix, and manages skills/languages & skills/styles."""

    def __init__(self, skills_dir: Optional[Path] = None) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        self.lang_dir = self.skills_dir / "languages"
        self.style_dir = self.skills_dir / "styles"
        self.lang_dir.mkdir(parents=True, exist_ok=True)
        self.style_dir.mkdir(parents=True, exist_ok=True)

    def _layer_dir(self, layer: str) -> Path:
        if layer == "languages":
            return self.lang_dir
        if layer == "styles":
            return self.style_dir
        raise ValueError(f"Unknown skill layer: {layer}")

    def _load_one_file(self, path: Path, default_layer: str) -> Optional[Dict[str, Any]]:
        try:
            raw_text = path.read_text(encoding="utf-8")
        except Exception:
            return None
        meta, body = _split_frontmatter_and_body(raw_text)
        skill_id = str(meta.get("id") or path.stem)
        layer = str(meta.get("layer") or default_layer)
        rules_text, examples = _parse_sections(body)
        tokens = estimate_tokens(body)
        limit = LAYER_TOKEN_LIMITS.get(layer, 160)
        return {
            "id": skill_id,
            "aliases": [str(a) for a in (meta.get("aliases") or [])],
            "name": str(meta.get("name") or skill_id),
            "layer": layer,
            "icon": str(meta.get("icon") or ("🌐" if layer == "languages" else "✨")),
            "description": str(meta.get("description") or ""),
            "source_langs": [str(s) for s in (meta.get("source_langs") or ["*"])],
            "target_langs": [str(t) for t in (meta.get("target_langs") or ["*"])],
            "auto_match_genres": [str(g) for g in (meta.get("auto_match_genres") or [])],
            "is_builtin": bool(meta.get("is_builtin", False)),
            "rules_text": rules_text,
            "examples": examples,
            "body": body,
            "tokens": tokens,
            "token_limit": limit,
            "token_warning": tokens > limit,
            "file_path": str(path),
            "content_hash": hashlib.sha1(raw_text.encode("utf-8")).hexdigest()[:8],
        }

    def list_skills(self) -> Dict[str, List[Dict[str, Any]]]:
        languages: List[Dict[str, Any]] = []
        styles: List[Dict[str, Any]] = []
        for p in sorted(self.lang_dir.glob("*.md")):
            item = self._load_one_file(p, "languages")
            if item:
                languages.append(item)
        for p in sorted(self.style_dir.glob("*.md")):
            item = self._load_one_file(p, "styles")
            if item:
                styles.append(item)

        # Keep natural_standard first for predictable UI ordering
        styles.sort(key=lambda s: (0 if s["id"] == "natural_standard" else (1 if s["is_builtin"] else 2), s["name"]))
        languages.sort(key=lambda s: (1 if s["id"] == "universal_auto" else 0, 0 if s["is_builtin"] else 1, s["name"]))
        return {"languages": languages, "styles": styles}

    def get_skill(self, layer: str, skill_id: str) -> Optional[Dict[str, Any]]:
        all_s = self.list_skills().get(layer, [])
        for s in all_s:
            if s["id"] == skill_id or skill_id in s.get("aliases", []):
                return s
        return None

    def resolve_active_skills(
        self,
        source_lang: str,
        target_lang: str,
        style_skill_id: str = "natural_standard",
        lang_skill_override: str = "auto",
        source_lang_name: str = "",
        target_lang_name: str = "",
    ) -> Dict[str, Any]:
        """Compose Layer 1 (Language Bridge) + Layer 2 (Style Pack) into fixed KV-Cache prefix."""
        catalog = self.list_skills()
        lang_skills = catalog["languages"]
        style_skills = catalog["styles"]

        # Resolve Layer 1 Language Bridge
        lang_skill: Optional[Dict[str, Any]] = None
        ov = (lang_skill_override or "auto").strip()
        if ov.lower() in ("off", "none"):
            lang_skill = None
        elif ov.lower() != "auto":
            lang_skill = next((s for s in lang_skills if s["id"] == ov), None)

        if lang_skill is None and ov.lower() not in ("off", "none"):
            scored: List[Tuple[int, Dict[str, Any]]] = []
            for s in lang_skills:
                s_sc = _lang_match_score(s.get("source_langs", []), source_lang)
                t_sc = _lang_match_score(s.get("target_langs", []), target_lang)
                if s_sc > 0 and t_sc > 0:
                    # Exact target + exact source gets top priority (4); wildcard->exact gets 3; wildcard->wildcard gets 2
                    scored.append((s_sc * 2 + t_sc * 3 + (1 if s["id"] != "universal_auto" else 0), s))
            if scored:
                scored.sort(key=lambda pair: -pair[0])
                lang_skill = scored[0][1]

        # Resolve Layer 2 Style Pack (handles legacy tone names cleanly via aliases)
        req_style = (style_skill_id or "natural_standard").strip()
        style_skill = next(
            (s for s in style_skills if s["id"] == req_style or req_style in s.get("aliases", [])),
            None,
        )
        if style_skill is None and style_skills:
            style_skill = next((s for s in style_skills if s["id"] == "natural_standard"), style_skills[0])

        src_disp = source_lang_name or source_lang.upper()
        tgt_disp = target_lang_name or target_lang.upper()

        prefix_parts: List[str] = []
        if lang_skill:
            rendered_lang = (
                lang_skill["body"]
                .replace("{source_lang_name}", src_disp)
                .replace("{target_lang_name}", tgt_disp)
            )
            prefix_parts.append(f"[L1]\n{rendered_lang}")
        if style_skill:
            prefix_parts.append(f"[L2]\n{style_skill['body']}")

        combined_prefix = "\n\n".join(prefix_parts).strip()
        sig_seed = (
            f"{lang_skill['id'] if lang_skill else 'none'}:{lang_skill['content_hash'] if lang_skill else '0'}"
            f"|{style_skill['id'] if style_skill else 'none'}:{style_skill['content_hash'] if style_skill else '0'}"
        )
        signature = hashlib.sha1(sig_seed.encode("utf-8")).hexdigest()[:16]

        return {
            "lang_skill": lang_skill,
            "style_skill": style_skill,
            "combined_prefix": combined_prefix,
            "combined_tokens": estimate_tokens(combined_prefix),
            "skill_signature": signature,
        }

    def save_skill(self, layer: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        target_dir = self._layer_dir(layer)
        raw_id = payload.get("id") or payload.get("name") or "custom_skill"
        skill_id = _slugify_id(raw_id)
        existing = self.get_skill(layer, skill_id)

        meta: Dict[str, Any] = {
            "id": skill_id,
            "name": str(payload.get("name") or skill_id).strip(),
            "layer": layer,
            "icon": str(payload.get("icon") or ("🌐" if layer == "languages" else "✨")).strip(),
            "description": str(payload.get("description") or "").strip(),
        }
        if payload.get("aliases"):
            meta["aliases"] = [str(a) for a in payload["aliases"]]
        elif existing and existing.get("aliases"):
            meta["aliases"] = existing["aliases"]

        if layer == "languages":
            src_list = payload.get("source_langs") or ["*"]
            tgt_list = payload.get("target_langs") or ["*"]
            if isinstance(src_list, str):
                src_list = [x.strip() for x in src_list.split(",") if x.strip()] or ["*"]
            if isinstance(tgt_list, str):
                tgt_list = [x.strip() for x in tgt_list.split(",") if x.strip()] or ["*"]
            meta["source_langs"] = src_list
            meta["target_langs"] = tgt_list
        else:
            genres = payload.get("auto_match_genres") or []
            if isinstance(genres, str):
                genres = [x.strip() for x in genres.split(",") if x.strip()]
            meta["auto_match_genres"] = genres
            meta["source_langs"] = payload.get("source_langs") or ["*"]

        meta["is_builtin"] = bool(existing["is_builtin"]) if existing else False

        md_text = _render_markdown(
            meta=meta,
            rules_text=str(payload.get("rules_text") or ""),
            examples=list(payload.get("examples") or []),
        )
        out_path = target_dir / f"{skill_id}.md"
        atomic_write_text(out_path, md_text)
        loaded = self._load_one_file(out_path, layer)
        if not loaded:
            raise ValueError("Không thể đọc lại file Skill vừa lưu.")
        return loaded

    def delete_skill(self, layer: str, skill_id: str) -> None:
        item = self.get_skill(layer, skill_id)
        if not item:
            raise FileNotFoundError(f"Không tìm thấy skill '{skill_id}' trong {layer}.")
        if item.get("is_builtin"):
            raise PermissionError("Không được phép xóa Skill hệ thống mặc định (is_builtin: true).")
        Path(item["file_path"]).unlink(missing_ok=True)

    def export_skill(
        self,
        layer: str,
        skill_id: str,
        fmt: str = "md",
        glossary_rows: Optional[List[Dict[str, str]]] = None,
    ) -> Tuple[str, str, bytes]:
        item = self.get_skill(layer, skill_id)
        if not item:
            raise FileNotFoundError(f"Skill '{skill_id}' not found")
        md_bytes = Path(item["file_path"]).read_bytes()
        if fmt.lower() == "zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("style.md" if layer == "styles" else f"{item['id']}.md", md_bytes)
                csv_buf = io.StringIO()
                writer = csv.writer(csv_buf)
                writer.writerow(["source_term", "target_term", "note"])
                for r in glossary_rows or []:
                    writer.writerow([r.get("source_term", ""), r.get("target_term", ""), r.get("note", "")])
                zf.writestr("glossary_template.csv", csv_buf.getvalue().encode("utf-8-sig"))
            return f"{item['id']}_skill_pack.zip", "application/zip", buf.getvalue()
        return f"{item['id']}.md", "text/markdown; charset=utf-8", md_bytes

    def import_skill(self, filename: str, raw_bytes: bytes) -> Dict[str, Any]:
        fn_low = (filename or "").lower()
        md_content = ""
        imported_glossary: List[Dict[str, str]] = []

        if fn_low.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
                for nm in zf.namelist():
                    if nm.lower().endswith(".md") and not md_content:
                        md_content = zf.read(nm).decode("utf-8", errors="replace")
                    elif nm.lower().endswith(".csv"):
                        txt = zf.read(nm).decode("utf-8-sig", errors="replace")
                        reader = csv.DictReader(io.StringIO(txt))
                        for row in reader:
                            if row.get("source_term") and row.get("target_term"):
                                imported_glossary.append(
                                    {
                                        "source_term": row["source_term"],
                                        "target_term": row["target_term"],
                                        "note": row.get("note", ""),
                                    }
                                )
        else:
            md_content = raw_bytes.decode("utf-8", errors="replace")

        if not md_content.strip():
            raise ValueError("File import không chứa nội dung Markdown hợp lệ.")

        meta, body = _split_frontmatter_and_body(md_content)
        layer = str(meta.get("layer") or "styles")
        if layer not in ("languages", "styles"):
            layer = "styles"
        rules_text, examples = _parse_sections(body)
        if not rules_text and body:
            rules_text = body
        skill_id = _slugify_id(str(meta.get("id") or Path(filename).stem))
        existing = self.get_skill(layer, skill_id)
        if existing and existing.get("is_builtin"):
            skill_id = f"{skill_id}_custom"

        saved = self.save_skill(
            layer,
            {
                "id": skill_id,
                "name": meta.get("name") or skill_id,
                "icon": meta.get("icon") or ("🌐" if layer == "languages" else "✨"),
                "description": meta.get("description") or "Imported Custom Skill",
                "source_langs": meta.get("source_langs") or ["*"],
                "target_langs": meta.get("target_langs") or ["*"],
                "auto_match_genres": meta.get("auto_match_genres") or [],
                "rules_text": rules_text,
                "examples": examples,
            },
        )
        return {"skill": saved, "glossary_rows": imported_glossary}


def build_ai_skill_gen_prompt(
    idea: str,
    source_lang_name: str,
    target_lang_name: str,
    layer: str = "styles",
) -> Tuple[str, str]:
    """Prompt any local model to synthesize a compact <=140-token Skill definition cleanly."""
    sys_p = (
        "You are a Game Localization Designer creating a compact reusable translation Skill.\n"
        "Output ONLY clean structured lines:\n"
        "NAME|<short Vietnamese title <= 32 chars>\n"
        "ICON|<1 emoji>\n"
        "DESC|<1 short Vietnamese sentence>\n"
        "RULE|<concise rule bullet <= 100 chars> (write 3 to 4 RULE| lines)\n"
        "EX|<source sentence>|<target translation> (write EXACTLY 3 EX| lines)"
    )
    usr_p = (
        f"Layer: {layer} ({source_lang_name} -> {target_lang_name})\n"
        f"User idea: {idea.strip()}\n"
        f"Keep rules and examples crisp so total fits under 140 tokens."
    )
    return sys_p, usr_p


def parse_ai_generated_skill(raw_text: str, idea: str, layer: str = "styles") -> Dict[str, Any]:
    """Parse AI-generated NAME/ICON/DESC/RULE/EX lines into UI-ready payload."""
    name = idea.strip()[:32] or "Custom AI Skill"
    icon = "🌐" if layer == "languages" else "✨"
    desc = idea.strip()[:120]
    rules: List[str] = []
    examples: List[Dict[str, str]] = []

    cleaned = re.sub(r"<think>.*?</think>", "", raw_text or "", flags=re.DOTALL | re.IGNORECASE)
    for ln in cleaned.splitlines():
        s = re.sub(r"^[-*•]\s*", "", ln.strip()).replace("**", "")
        if "|" not in s:
            continue
        parts = [p.strip() for p in s.split("|")]
        tag = parts[0].upper()
        if tag == "NAME" and len(parts) >= 2 and parts[1]:
            name = parts[1][:40]
        elif tag == "ICON" and len(parts) >= 2 and parts[1]:
            icon = parts[1][:4]
        elif tag == "DESC" and len(parts) >= 2 and parts[1]:
            desc = parts[1][:160]
        elif tag == "RULE" and len(parts) >= 2 and parts[1]:
            rules.append(parts[1])
        elif tag == "EX" and len(parts) >= 3 and parts[1] and parts[2]:
            examples.append({"source": parts[1], "target": parts[2]})

    if not rules:
        rules = [
            f"Áp dụng đúng văn phong: {idea.strip()[:80]}.",
            "Dịch tự nhiên theo ngữ cảnh game, tránh lặp chữ hoặc cứng nhắc.",
            "Bảo toàn 100% thẻ __TAG_x__, __NUM_x__ và __NL__.",
        ]
    while len(examples) < 3:
        examples.append({"source": "New Game", "target": "Trò chơi mới"})

    return {
        "name": name,
        "icon": icon,
        "description": desc,
        "rules_text": "\n".join(f"- {r}" for r in rules[:4]),
        "examples": examples[:3],
    }

"""Telegram HTML safety: sanitize() keeps only the tags Telegram supports and escapes every other '<', '>', '&';
to_plain() for the parse-error fallback; validate() mimics Telegram's parser (used by tests)."""
import re, html
ALLOWED = ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "span", "blockquote")
_TAG = re.compile(r'</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|tg-spoiler|blockquote)>|<a href="[^"<>]*">|</a>|<span class="tg-spoiler">|</span>')
_ENT = re.compile(r'&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);')
def _esc(chunk):
    out, i = [], 0
    for m in _ENT.finditer(chunk):
        out.append(html.escape(chunk[i:m.start()], quote=False)); out.append(m.group(0)); i = m.end()
    out.append(html.escape(chunk[i:], quote=False)); return "".join(out)
def sanitize(text):
    """Escape everything except well-formed allowed tags; existing entities are kept (no double escaping)."""
    if text is None: return text
    text = str(text); out, i = [], 0
    for m in _TAG.finditer(text):
        out.append(_esc(text[i:m.start()])); out.append(m.group(0)); i = m.end()
    out.append(_esc(text[i:])); s = "".join(out)
    return s if not validate(s) else html.escape(to_plain(text), quote=False)   # unbalanced tags → all escaped
def to_plain(text):
    return html.unescape(re.sub(r"</?[a-zA-Z][a-zA-Z-]*(?:\s[^<>]*)?>", "", str(text or "")))
def validate(text):
    """Return a list of problems Telegram would reject (empty = OK)."""
    errs, stack, i, t = [], [], 0, str(text or "")
    while i < len(t):
        ch = t[i]
        if ch == "<":
            m = re.match(r'<(/?)([a-zA-Z-]+)(\s[^<>]*)?>', t[i:])
            if not m: errs.append(f"stray '<' at {i}: {t[i:i+12]!r}"); i += 1; continue
            close, name = m.group(1) == "/", m.group(2).lower()
            if name not in ALLOWED: errs.append(f"unsupported tag <{name}> at {i}")
            elif close:
                if not stack or stack[-1] != name: errs.append(f"unbalanced </{name}> at {i}")
                else: stack.pop()
            else: stack.append(name)
            i += len(m.group(0)); continue
        if ch == "&":
            m = _ENT.match(t, i)
            if not m: errs.append(f"raw '&' at {i}")
            else: i = m.end(); continue
        if ch == ">": errs.append(f"raw '>' at {i}")
        i += 1
    if stack: errs.append(f"unclosed tags {stack}")
    return errs
def is_parse_error(resp_text): return "can't parse entities" in (resp_text or "") or "Unsupported start tag" in (resp_text or "")

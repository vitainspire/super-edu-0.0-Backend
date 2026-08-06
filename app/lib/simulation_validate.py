"""Static validation for generated simulation HTML. Mirrors
frontend/lib/simulation-validate.ts — pure-text/regex checks only, no browser,
no I/O (the original's real headless-Chrome layout probe was deliberately not
ported there either, since it needs a Chrome binary this backend doesn't have).
"""
import re

from .simulation_lottie import LOTTIE_NAMES, SUGGESTED_CELEBRATION, used_lottie_names


def extract_html(text: str) -> str:
    fenced = re.search(r"```(?:html)?\s*([\s\S]*?)```", text, re.I)
    return (fenced.group(1) if fenced else text).strip()


def _sections_of(html: str, tag: str) -> list[str]:
    return re.findall(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", html, re.I)


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


def _strip_js_comments(js: str) -> str:
    js = re.sub(r"/\*[\s\S]*?\*/", "", js)
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


def validate_simulation_html(html: str) -> list[str]:
    problems: list[str] = []
    styles = _strip_css_comments("\n".join(_sections_of(html, "style")))
    scripts = _strip_js_comments("\n".join(_sections_of(html, "script")))
    html_no_comments = re.sub(r"<!--[\s\S]*?-->", "", html)

    css_in_js = re.search(r"^\s*@(keyframes|media|import|font-face)\b", scripts, re.I | re.M)
    if css_in_js:
        problems.append(
            f'A CSS "@{css_in_js.group(1)}" block is inside the <script> tag. That is a JavaScript syntax error '
            "and stops the whole page from working. Move every CSS rule into the <style> tag."
        )

    external = re.findall(
        r"""@import|(?:src|href)\s*=\s*["']https?://[^"']+|url\(\s*["']?https?://[^)"']+""",
        html_no_comments, re.I,
    )
    if external:
        uniq = list(dict.fromkeys(external))[:5]
        problems.append(f"The page loads external resources, but it must work fully offline. Remove these: {', '.join(uniq)}")

    bad_fn = re.findall(r"[a-zA-Z-]+_[a-zA-Z_-]*\s*\(", styles)
    if bad_fn:
        uniq = list(dict.fromkeys(bad_fn))
        problems.append(
            f"Invalid CSS function name(s): {', '.join(uniq)}. CSS function names never contain underscores. "
            "Gradients must look like: linear-gradient(to bottom right, #A7ECEE, #F7CAC9)"
        )

    opens, closes = styles.count("{"), styles.count("}")
    if opens != closes:
        problems.append(f'The CSS has unbalanced braces ({opens} "{{" vs {closes} "}}"). Fix the stylesheet structure.')

    if re.search(r"""draggable\s*=\s*["']true|\bdragstart\b|\bdataTransfer\b""", html_no_comments, re.I):
        problems.append(
            "The page uses the native HTML5 Drag and Drop API, which does not work on touchscreens. "
            "Use plain click/tap (or pointer events) instead."
        )

    if not re.search(r"<!doctype html", html_no_comments, re.I):
        problems.append("The document must start with <!DOCTYPE html>.")

    if LOTTIE_NAMES:
        unknown = [n for n in used_lottie_names(html_no_comments) if n not in LOTTIE_NAMES]
        if unknown:
            problems.append(
                f"playLottie() is called with animation name(s) that do not exist: {', '.join(f'{chr(34)}{n}{chr(34)}' for n in unknown)}. "
                f"The only valid names are: {', '.join(LOTTIE_NAMES)}."
            )
        if re.search(r"\blottie\s*\.\s*loadAnimation|\bbodymovin\b", scripts, re.I):
            problems.append(
                f"Do not set up the animation player yourself. Just call playLottie('{SUGGESTED_CELEBRATION}') — "
                "the player is bundled into the page for you."
            )
        if re.search(r"function\s+playLottie|(?:var|let|const)\s+playLottie|window\s*\.\s*playLottie\s*=", scripts):
            problems.append("playLottie is already defined for you. Delete your own definition of it and just call it.")

        party_keyframes = list(dict.fromkeys(re.findall(
            r"@keyframes\s+([\w-]*(?:sparkle|confetti|celebrat|yay|congrat|cheer|tada|firework|twinkle|glitter|hooray|party|welldone)[\w-]*)",
            styles, re.I,
        )))
        if party_keyframes:
            named = ", ".join(f"@keyframes {n}" for n in party_keyframes)
            problems.append(
                f"These CSS animations are doing celebration work that playLottie already handles: {named}. "
                f"Delete them and call playLottie('{SUGGESTED_CELEBRATION}') instead. Keep CSS animation only for "
                "quick tap feedback and for moving something to a position worked out at run time."
            )

    # Mirrors the TS regex exactly: common pictographic ranges plus the
    # variation-selector/ZWJ characters emoji sequences are built from.
    emoji_re = "[\\U0001F300-\\U0001FAFF\\u2600-\\u27BF\\u2B00-\\u2BFF\\u2190-\\u21FF\\uFE0F\\u200D]"
    emoji = re.findall(emoji_re, html_no_comments)
    if emoji:
        uniq = list(dict.fromkeys(emoji))[:10]
        problems.append(
            f"The page uses emoji ({' '.join(uniq)}), which is not allowed. Draw every object as a CSS shape "
            "(circles, rounded rectangles, conic-gradient wedges, etc.) instead, and use plain text for any feedback/labels."
        )

    return problems

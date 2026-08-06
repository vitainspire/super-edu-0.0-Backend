import base64
import re
import time
import httpx
from fastapi import APIRouter, HTTPException, Request

from ..lib.ai import call_ai, generate_illustration
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_vision_rate_limit
from ..lib.simulation_lottie import CELEBRATION_RULES, inject_lottie
from ..lib.simulation_validate import extract_html, validate_simulation_html
from ..lib.supabase_clients import create_admin_client
from ..lib.schemas import GenerateSimulationSchema

router = APIRouter()

_MAX_ATTEMPTS = 3
_SIMULATIONS_BUCKET = "simulations"

# Ported from frontend/app/api/generate-simulation/route.ts, with changes: (1)
# the "small amount of text" and "compact, centered" constraints are explicit
# per this app's actual usage — the page is shown inside an iframe inside a
# modal, not full-page, and there's a teacher talking rather than a child
# reading silently; (2) LOOK mirrors EduTeach's own design system verbatim
# instead of generic kids'-app clip art; (3) celebrations are Lottie rather
# than the model hand-rolling CSS keyframes or emoji. Uses call_ai() here
# (unlike the TS original's raw fetch), so this gets the shared circuit
# breaker/fallback for free.
_SYSTEM_PROMPT_BASE = """You write single-file interactive simulations that a TEACHER operates at the front of a classroom while narrating to students (ages 6-10) — this is a presentation aid for the teacher to tap through and talk over, not a self-directed kid's app or a developer demo. Skin it to match the EduTeach teacher portal's own design system exactly.

Produce ONE complete, self-contained HTML document.

WHO IS TAPPING, AND WHY THAT CHANGES THE DESIGN
- ONE person — the teacher — taps through this in front of the whole class, the same way they'd flip pages in the Prep Sheet. Design for that single operator, not for many students exploring independently on their own devices.
- Structure the whole thing as 2-4 short STEPS in a fixed order, each ONE tap/action long. Do not build an open-ended sandbox the "player" pokes around in — a teacher mid-lesson needs a clear next tap, not a menu of options to figure out.
- Every step's on-screen text must be something the teacher would actually SAY OUT LOUD to the class, not generic UI copy. Bad: "Click to continue." Good: "How many are left if we take 2 away?" — phrase it as a question or line aimed at the room, exactly like a script line in the Prep Sheet's own "detail" fields.
- The LAST step must end on a short line phrased as a question for the teacher to put to the whole class right then — a handoff back to the teacher, not just a "Correct!" and a stop. Something like "Ask the class: what would happen with one more?"
- Reuse the EXACT numbers, character/object names, and scenario already given below from the lesson's Concept/Explore/Challenge — do not invent a different scenario or swap in new numbers. This must feel like the direct next moment of the same scene the teacher already explained, not a disconnected mini-game that happens to share a topic.

CONTENT AND TONE
- NO EMOJI ANYWHERE, EVER — not for objects, not for feedback, not for decoration, not in labels or button text. This is a hard rule with zero exceptions.
- Represent quantities with things kids recognize (apples, balloons, cookies, stars, animals, cricket balls, coins, rotis, laddoos) drawn as simple CSS shapes/divs — circles, rounded rectangles, conic-gradient wedges, simple layered shapes built from borders/border-radius/background-color — not bare abstract dots, and never emoji as a substitute for drawing them. A plain colored circle labelled "coin" is right; an emoji glyph is not.
- KEEP TEXT TO A MINIMUM — this is shown inside a modal with the teacher talking, not read silently by students. At most one short line visible at a time, and it should read like something the teacher says aloud (a question, a prompt to the class) rather than an instruction to a solo reader. Prefer showing the idea over describing it. Never a paragraph, never multiple lines stacked on screen at once.
- Use real numbers, names, and content matching the description exactly. Never use lorem ipsum or placeholder text.
- Celebrate success by calling playLottie(...) (see CELEBRATION ANIMATIONS below) plus a short plain-text phrase like "Yay!" or "Correct!" — never emoji, never a harsh red X or scolding tone; use an encouraging nudge instead ("Try again!"). No sound is available, so all feedback must be visual.{celebration_rules}

LAYOUT — this page renders inside a modal on a teacher's screen, not full-page:
- Design ONE compact, centered activity card, not an edge-to-edge app. Make body a flex container (display:flex; justify-content:center; align-items:center; min-height:100vh; margin:0) and put all content in a single wrapper with max-width around 480px (never wider than 560px), so it reads as one focused card sitting in the middle of the screen.
- It's fine — good, even — for the wrapper to be shorter than the viewport. Don't stretch content to fill unused vertical space, and don't design for a wide desktop layout.

LOOK — match the EduTeach teacher portal's actual design system, not generic kids'-app clip art:
- Page background: warm ivory paper, #F1EDDF. Never white, never a gradient background.
- EVERY card/tile/button gets a BOLD 2px solid near-black outline, #1B180F — this is the app's signature "bold-outline, flat matte" look. NO drop shadows, NO glow, NO gloss/gradient fills anywhere. Depth comes only from the outline, never from shadow.
- Corners are heavily rounded: 20-24px radius on cards, 14-16px on buttons/tiles, fully round (pill/circle) for badges and counters.
- Ink/text colors: #17140F (near-black) for headings and important text, #4A4740 (warm grey) for secondary/help text.
- Primary accent: deep forest green, #1F3D2C, with #2C5540 and #3E7A57 as supporting greens — used for primary buttons, active/selected states, and progress fills. This green is the app's signature color; use it as the main accent, not a rare highlight.
- For grouping/categorizing things (a team, a bucket, a row) pick ONE flat pastel tile color per group, always with the bold #1B180F outline: mint #DCEEE1, peach #F4D6C0, gold #F7EFC4, sky blue #D6E3F3, violet #DED3F2, pink #F4D7E1. Never use these as the page background.
- Buttons: solid fill (ink #17140F or forest #1F3D2C), white bold text, fully rounded, 2px outline optional on the dark fill itself (skip it if fill already reads clearly), NO gradient — matches a flat "sticker" look, not a glossy app-store button.
- Small tags/badges/counters: white or near-white pill background, thin bold #1B180F outline, bold ink text.
- Typography: font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif for EVERYTHING — no Comic Sans, no handwriting/cursive fonts, no serif. Headings and big numbers are very heavy weight (800-900) with tight letter-spacing, like a confident geometric display face; body/instruction text is 500-600 weight. The friendliness comes from color, roundness, and warmth — not from a cartoonish font.
- Large type: body text at least 20px, numbers and key labels much bigger (heavy weight). Tap targets at least 48px, since small fingers use these on tablets.

INTERACTION
- The default interaction is a plain click/tap by the teacher advancing the current step — clicking something should immediately act (move it, count it, reveal it, toggle it) and, where it fits the step, surface the next line for the teacher to read. Never implement a "press and hold, then follow the cursor/finger" drag gesture unless the description explicitly uses the word "drag". If it does, use pointer events (pointerdown/pointermove/pointerup, setPointerCapture) — never the native HTML5 Drag and Drop API (draggable="true", dragstart/drop, DataTransfer), which is unreliable in sandboxes and unsupported on touchscreens.
- Don't build multiple independent interactive widgets on one screen for the teacher to choose between — one clear next tap at a time, matching the fixed step sequence above.

HARD TECHNICAL RULES — breaking any of these breaks the page:
1. Put ALL CSS inside the <style> tag and ALL JavaScript inside the <script> tag. NEVER put CSS (@keyframes, @media, selectors, style rules) inside <script> — that is a JavaScript syntax error that kills the entire page. Every @keyframes block belongs in <style>, always.
2. Absolutely no network requests: no @import, no <link> to fonts or stylesheets, no external <script src>, no url(https://...), no remote images. Nothing outside this one file. The page must work fully offline.
3. Write only valid CSS. Gradients must be written exactly like: linear-gradient(to bottom right, #A7ECEE, #F7CAC9). CSS function names never contain underscores.
4. Lay the page out in normal document flow with flexbox or grid and gap. Use position: absolute only when an element genuinely must move freely over a track (for example a character sliding along a number line), and only inside a parent with position: relative that reserves enough space for it. An absolutely positioned element must never end up covering a button, an input, or any text.
5. Never set a fixed height (or width) on anything containing text. Use padding plus min-height so text can never overflow its container.
6. Nothing may ever be cut off or run off the edge of the screen. Any row of items (number tiles, counters, cards) must use flex-wrap: wrap so it wraps onto a second line instead of overflowing, and containers should size to their content rather than forcing a fixed width. The page must never scroll sideways.
7. Make sure the JavaScript actually runs: no stray tokens, balanced braces and quotes. The page must render correctly with zero console errors.
8. To draw a round object split into fraction slices (pizza, roti, laddoo, pie, cake), use a div with border-radius: 50% and a conic-gradient background — for example background: conic-gradient(#ff9900 0deg 90deg, #f7d28c 90deg 360deg) shades one quarter. Build the gradient string in JavaScript to shade any number of slices, and draw the dividing lines with a separate overlay using repeating-conic-gradient. Do NOT try to build slices out of rotated rectangles with clip-path — that produces broken diamond shapes that spill outside the circle. For rectangular fractions (chocolate bars, ribbons) just use a flex row of equal-width divs.
9. Respect prefers-reduced-motion by shortening your own CSS animations (tap feedback, character motion). playLottie already handles this for celebrations on its own — nothing to do there.

Output ONLY the raw HTML document, starting with <!DOCTYPE html>. No markdown code fences, no explanation before or after."""

SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE.replace("{celebration_rules}", CELEBRATION_RULES)


def _bullet_lines(bullets) -> str:
    lines = []
    for b in (bullets or []):
        text = b.get("text", "")
        detail = b.get("detail")
        lines.append(f"- {text}" + (f" — {detail}" if detail else ""))
    return "\n".join(lines)


def _describe_lesson(topic: str, subtopic: str, subject: str, grade: str, lesson: dict) -> str:
    parts = [
        f"Topic: {topic}" + (f" — {subtopic}" if subtopic else ""),
        f"Subject: {subject}",
        f"Grade: {grade}",
        "",
        "This is the exact worked example the teacher just explained to the class. Build ONE small simulation the teacher taps through WHILE NARRATING TO THE WHOLE CLASS — a direct continuation of this same scene, same numbers, same names, not a new scenario and not a new topic.",
        "",
        "Concept (the idea being taught):",
        _bullet_lines(lesson.get("concept")),
        "",
        "Explore scenario (the real-life scene the class stepped into):",
        _bullet_lines((lesson.get("explore") or {}).get("points")),
    ]
    challenge = lesson.get("challenge") or {}
    if challenge.get("activity"):
        parts += ["", f'Challenge activity ("{challenge["activity"]}"):', _bullet_lines(challenge.get("points"))]
    if lesson.get("materialsUsed"):
        parts += ["", f"Materials named in the lesson: {', '.join(lesson['materialsUsed'])}"]
    parts += [
        "",
        "Pick ONE clear idea from the above and build a short, fixed sequence of 2-4 teacher-taps around it (tap, count, build, reveal) — each one paired with a line the teacher would actually say or ask the class at that exact moment. End on a line phrased as a question the teacher poses to the whole room, handing the moment back to them rather than just declaring success.",
    ]
    return "\n".join(p for p in parts if p is not None)


def _describe_blackboard_scene(topic: str, subtopic: str, subject: str, grade: str, lesson: dict) -> str:
    concept = (lesson.get("concept") or [None])[0]
    explore_points = (lesson.get("explore") or {}).get("points") or [None]
    explore = explore_points[0]
    example_parts = []
    if concept:
        example_parts.append(concept.get("text", "") + (f" — {concept['detail']}" if concept.get("detail") else ""))
    if explore:
        example_parts.append(explore.get("text", "") + (f" — {explore['detail']}" if explore.get("detail") else ""))
    example = " ".join(p for p in example_parts if p)
    fallback = f"{topic}" + (f", focusing on {subtopic}" if subtopic else "")

    return f"""A richly detailed, teacher-facing CHALK-ON-BLACKBOARD illustration — the exact scene a teacher would draw on a real classroom blackboard to explain this, in white and colored chalk on a dark green/black board background (visible chalk texture, slightly rough hand-drawn line quality, not a clean vector graphic).

Topic: {topic}{f' — {subtopic}' if subtopic else ''} ({subject}, Grade {grade})
It must accurately and thoroughly depict this worked example: "{example or fallback}"

Requirements:
- VERY DESCRIPTIVE: don't just sketch one bare object — build a small complete scene with concrete, countable real-life objects (coins, rotis, fruits, stick figures, a number line drawn in chalk, small labels) that together walk through the actual example step by step, exactly as a teacher narrating it aloud would draw it piece by piece.
- Quantities must be EXACTLY right and countable — if the example says a specific number, draw exactly that many, correctly grouped.
- Use only chalk-plausible marks: simple outlines, cross-hatching for shading, dotted/dashed lines for division, stick-figure people — nothing photorealistic or painterly.
- A few short handwritten-style chalk labels or numbers are welcome; keep any text minimal, correctly spelled, and secondary to the drawing itself.
- Plain dark blackboard background (near-black or dark slate green), no classroom walls, no border, no watermark or logo.
Clean enough to read from the back of a classroom, but full of the concrete detail that makes the idea click at a glance."""


# POST /api/generate-simulation
@router.post("/generate-simulation")
async def generate_simulation(body: GenerateSimulationSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    t0 = time.time()
    subtopic = body.subtopic or ""
    description = _describe_lesson(body.topic, subtopic, body.subject, body.grade, body.lesson)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": description},
    ]

    html = ""
    problems: list[str] = []

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            text = await call_ai(messages, {"max_tokens": 8192, "json_mode": False, "timeout_s": 60})
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        candidate = extract_html(text)
        if "<html" not in candidate.lower():
            problems = ["You did not return a complete HTML document starting with <!DOCTYPE html>."]
        else:
            html = candidate
            problems = validate_simulation_html(html)

        if not problems:
            break
        if attempt == _MAX_ATTEMPTS:
            break  # save the latest version anyway, matching the original CLI script's behavior

        messages.append({"role": "assistant", "content": html or "(no document)"})
        messages.append({
            "role": "user",
            "content": (
                "That document has these problems:\n\n"
                + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(problems))
                + "\n\nReturn the COMPLETE corrected HTML document with every problem fixed. Output only the raw HTML, no code fences."
            ),
        })

    if not html:
        raise HTTPException(status_code=500, detail="Could not generate a usable simulation")

    admin = create_admin_client()

    # Idempotent — same pattern as any Storage bucket in this app: created on
    # first use rather than via a migration.
    try:
        admin.storage.create_bucket(_SIMULATIONS_BUCKET, options={"public": True})
    except Exception:
        pass  # already exists

    # No DB table — the Storage object IS the record. Path is deterministic
    # (classId/prepMaterialId.html), so the client derives the same public URL
    # and checks existence itself, without a `simulations` table or migration.
    # upsert=true makes "Regenerate" replace the existing file at the same path.
    doc = inject_lottie(html)
    storage_path = f"{body.classId}/{body.prepMaterialId}.html"
    try:
        admin.storage.from_(_SIMULATIONS_BUCKET).upload(
            storage_path, doc.encode("utf-8"), {"content-type": "text/html", "upsert": "true"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}")

    url = admin.storage.from_(_SIMULATIONS_BUCKET).get_public_url(storage_path)

    # Second artifact, same bucket, same deterministic-path pattern — best
    # effort. A failure here (model declined, timeout) never fails the
    # request; the simulation itself has already been saved above.
    blackboard_image_url = None
    try:
        illustration = await generate_illustration(
            _describe_blackboard_scene(body.topic, subtopic, body.subject, body.grade, body.lesson),
            timeout_s=25,
        )
        if illustration and illustration.get("url"):
            data_url = illustration["url"]
            match = re.match(r"^data:([^;]+);base64,([\s\S]+)$", data_url)
            if match:
                content_type, b64 = match.group(1), match.group(2)
                image_bytes = base64.b64decode(b64)
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(data_url)
                    image_bytes = resp.content
                content_type = "image/png"
            image_path = f"{body.classId}/{body.prepMaterialId}-blackboard.png"
            admin.storage.from_(_SIMULATIONS_BUCKET).upload(
                image_path, image_bytes, {"content-type": content_type, "upsert": "true"},
            )
            blackboard_image_url = admin.storage.from_(_SIMULATIONS_BUCKET).get_public_url(image_path)
    except Exception as e:
        print(f"[generate-simulation] blackboard image failed: {e}")

    api_log("generate-simulation", ip, (time.time() - t0) * 1000, False, "ok")
    return {"url": url, "blackboardImageUrl": blackboard_image_url}

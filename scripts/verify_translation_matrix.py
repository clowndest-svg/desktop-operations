"""12 语种 × 10 句 = 120 条画面描述，逐条核可机检的约束，产出可比较的合格率。

为什么要这一份：r119 之前"多语言翻译准不准"只有六语种那一句「三只红猫坐在蓝色长椅上」，
一个孤例既不能算通过也不能算失败。这里把句子扩到 120 条，每条预先写死一组**机器能判**的
约束（数量 / 主体 / 颜色 / 左右位置 / 否定 / 动作 / 权重语法），跑完给的是分语种、分约束
两个方向的通过率，而不是一句"基本正常"。

走的是软件自己那个 `PromptTranslator`（含语言识别、截断检查、缓存），不是另写一套调用 ——
手搓一遍会把真正要测的那层跳过。

只接受回环地址：这一发要连着打上百次模型请求，不该出本机。

    .venv/Scripts/python.exe scripts/verify_translation_matrix.py \
        --base-url http://127.0.0.1:18181/v1 --model qwen2.5-7b
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.app.media_prompt import PromptTranslator
from jarvis.llm.openai_compat import OpenAiCompatClient, OpenAiCompatSettings


@dataclass
class Row:
    """一条用例的完整结果；失败也占一行，不许从回执里消失。"""

    scene: str
    language: str
    source: str
    english: str = ""
    provider: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    ok: bool = False
    seconds: float = 0.0
    error: str = ""


LANGUAGES: Final = ("zh", "en", "fr", "de", "es", "pt", "ja", "ko", "ru", "ar", "hi", "tr")

# 每句都带 4~6 条能机检的约束；判据只看**英文输出**，所以源语写歪一点只会让任务更难，
# 不会把打分打错。en 那一列走的是"输入已是英文"的跳过分支，单独计。
SCENES: Final[tuple[tuple[str, dict[str, str]], ...]] = (
    (
        "two_white_dogs",
        {
            "zh": "两只白狗在草地上跑。",
            "en": "Two white dogs run on the grass.",
            "fr": "Deux chiens blancs courent sur l'herbe.",
            "de": "Zwei weiße Hunde rennen auf dem Gras.",
            "es": "Dos perros blancos corren sobre la hierba.",
            "pt": "Dois cachorros brancos correm na grama.",
            "ja": "二匹の白い犬が芝生を走っている。",
            "ko": "흰색 개 두 마리가 잔디를 달린다.",
            "ru": "Две белые собаки бегут по траве.",
            "ar": "كلبان أبيضَان يركضان على العشب.",
            "hi": "दो सफेद कुत्ते घास पर दौड़ते हैं।",
            "tr": "İki beyaz köpek çimde koşuyor.",
        },
    ),
    (
        "boy_no_umbrella",
        {
            "zh": "一个男孩没有带伞。",
            "en": "A boy has no umbrella.",
            "fr": "Un garçon n'a pas de parapluie.",
            "de": "Ein Junge hat keinen Regenschirm.",
            "es": "Un niño no tiene paraguas.",
            "pt": "Um menino não tem guarda-chuva.",
            "ja": "男の子は傘を持っていない。",
            "ko": "소년은 우산을 갖고 있지 않다.",
            "ru": "У мальчика нет зонта.",
            "ar": "الصبي ليس لديه مظلة.",
            "hi": "एक लड़के के पास छाता नहीं है।",
            "tr": "Bir erkek çocuğunun şemsiyesi yok.",
        },
    ),
    (
        "house_left_of_tree",
        {
            "zh": "红房子在蓝树的左边。",
            "en": "The red house is on the left of the blue tree.",
            "fr": "La maison rouge est à gauche de l'arbre bleu.",
            "de": "Das rote Haus steht links vom blauen Baum.",
            "es": "La casa roja está a la izquierda del árbol azul.",
            "pt": "A casa vermelha fica à esquerda da árvore azul.",
            "ja": "赤い家は青い木の左にある。",
            "ko": "빨간 집은 파란 나무 왼쪽에 있다.",
            "ru": "Красный дом стоит слева от синего дерева.",
            "ar": "المنزل الأحمر على يسار الشجرة الزرقاء.",
            "hi": "लाल घर नीले पेड़ के बाईं ओर है।",
            "tr": "Kırmızı ev mavi ağacın solunda.",
        },
    ),
    (
        "woman_piano",
        {
            "zh": "一个女人正在弹钢琴。",
            "en": "A woman is playing the piano.",
            "fr": "Une femme joue du piano.",
            "de": "Eine Frau spielt Klavier.",
            "es": "Una mujer está tocando el piano.",
            "pt": "Uma mulher está tocando piano.",
            "ja": "女性がピアノを弾いている。",
            "ko": "한 여성이 피아노를 치고 있다.",
            "ru": "Женщина играет на пианино.",
            "ar": "امرأة تعزف على البيانو.",
            "hi": "एक महिला पियानो बजा रही है।",
            "tr": "Bir kadın piyano çalıyor.",
        },
    ),
    (
        "three_red_cats_bench",
        {
            "zh": "三只红猫坐在蓝色长椅上，没有帽子。",
            "en": "Three red cats sit on a blue bench without hats.",
            "fr": "Trois chats rouges sont assis sur un banc bleu, sans chapeaux.",
            "de": "Drei rote Katzen sitzen auf einer blauen Bank, ohne Hüte.",
            "es": "Tres gatos rojos están sentados en un banco azul, sin sombreros.",
            "pt": "Três gatos vermelhos estão sentados em um banco azul, sem chapéus.",
            "ja": "三匹の赤い猫が青いベンチに座っていて、帽子はありません。",
            "ko": "빨간 고양이 세 마리가 파란 벤치에 앉아 있고 모자는 없습니다.",
            "ru": "Три красных кота сидят на синей скамейке без шляп.",
            "ar": "ثلاث قطط حمراء تجلس على مقعد أزرق بدون قبعات.",
            "hi": "तीन लाल बिल्लियाँ नीली बेंच पर बैठी हैं, कोई टोपी नहीं है।",
            "tr": "Üç kırmızı kedi mavi bir bankta oturuyor, şapka yok.",
        },
    ),
    (
        "man_black_coat",
        {
            "zh": "男人穿着黑色外套和白色衬衫。",
            "en": "The man wears a black coat and a white shirt.",
            "fr": "L'homme porte un manteau noir et une chemise blanche.",
            "de": "Der Mann trägt einen schwarzen Mantel und ein weißes Hemd.",
            "es": "El hombre lleva un abrigo negro y una camisa blanca.",
            "pt": "O homem usa um casaco preto e uma camisa branca.",
            "ja": "男性は黒いコートと白いシャツを着ている。",
            "ko": "남성은 검은 코트와 흰 셔츠를 입고 있다.",
            "ru": "Мужчина в чёрном пальто и белой рубашке.",
            "ar": "يرتدي الرجل معطفا أسود وقميصا أبيض.",
            "hi": "व्यक्ति काला कोट और सफेद शर्ट पहने है।",
            "tr": "Adam siyah palto ve beyaz gömlek giyiyor.",
        },
    ),
    (
        "night_city_stars",
        {
            "zh": "夜晚的城市，天上有星星。",
            "en": "A city at night, with stars in the sky.",
            "fr": "Une ville la nuit, avec des étoiles dans le ciel.",
            "de": "Eine Stadt bei Nacht, mit Sternen am Himmel.",
            "es": "Una ciudad de noche, con estrellas en el cielo.",
            "pt": "Uma cidade à noite, com estrelas no céu.",
            "ja": "夜の街、空に星がある。",
            "ko": "밤의 도시, 하늘에 별이 있다.",
            "ru": "Ночной город, звёзды в небе.",
            "ar": "مدينة ليلية، ونجوم في السماء.",
            "hi": "रात का शहर, आसमान में तारे।",
            "tr": "Gece şehri, gökyüzünde yıldızlar.",
        },
    ),
    (
        "bird_on_branch",
        {
            "zh": "一只鸟站在树枝上。",
            "en": "A bird stands on a tree branch.",
            "fr": "Un oiseau est perché sur une branche.",
            "de": "Ein Vogel sitzt auf einem Ast.",
            "es": "Un pájaro está en una rama.",
            "pt": "Um pássaro está em um galho.",
            "ja": "一羽の鳥が枝にとまっている。",
            "ko": "새 한 마리가 나뭇가지에 앉아 있다.",
            "ru": "Птица сидит на ветке дерева.",
            "ar": "طائر يقف على غصن شجرة.",
            "hi": "एक पक्षी पेड़ की डाल पर बैठा है।",
            "tr": "Bir kuş dalın üzerinde duruyor.",
        },
    ),
    (
        "boy_girl_hands",
        {
            "zh": "一个男孩牵着一个女孩的手。",
            "en": "A boy holds a girl by the hand.",
            "fr": "Un garçon tient la main d'une fille.",
            "de": "Ein Junge hält ein Mädchen an der Hand.",
            "es": "Un niño toma de la mano a una niña.",
            "pt": "Um menino segura a mão de uma menina.",
            "ja": "男の子が女の子の手を握っている。",
            "ko": "소년이 소녀의 손을 잡고 있다.",
            "ru": "Мальчик держит девочку за руку.",
            "ar": "صبي يمسك بيد فتاة.",
            "hi": "लड़की का हाथ लड़का पकड़े है।",
            "tr": "Bir erkek çocuk bir kızın elini tutuyor.",
        },
    ),
    (
        "weighted_car_bridge",
        {
            "zh": "(red car:1.2) 在桥上，路上没有行人。",
            "en": "(red car:1.2) on a bridge, no pedestrians on the road.",
            "fr": "(red car:1.2) sur un pont, pas de piétons sur la route.",
            "de": "(red car:1.2) auf einer Brücke, keine Fußgänger auf der Straße.",
            "es": "(red car:1.2) sobre un puente, sin peatones en la carretera.",
            "pt": "(red car:1.2) sobre uma ponte, sem pedestres na estrada.",
            "ja": "(red car:1.2) が橋の上にある、道に歩行者はいない。",
            "ko": "(red car:1.2) 다리 위에, 도로에 행인이 없다.",
            "ru": "(red car:1.2) на мосту, на дороге нет пешеходов.",
            "ar": "(red car:1.2) على جسر، ولا مشاة على الطريق.",
            "hi": "(red car:1.2) पुल पर है, सड़क पर कोई पैदल यात्री नहीं।",
            "tr": "(red car:1.2) bir köprü üzerinde, yolda yaya yok.",
        },
    ),
)

# 每个场景要机检的东西，全部针对**英文输出**。
CHECKS: Final[dict[str, dict[str, str]]] = {
    "two_white_dogs": {
        "count_two": r"\b(two|2)\b",
        "subject_dog": r"\bdogs?\b",
        "color_white": r"\bwhite\b",
        "action_run": r"\b(run|running|runs)\b",
        "place_grass": r"\b(grass|lawn|meadow)\b",
    },
    "boy_no_umbrella": {
        "subject_boy": r"\bboy\b",
        "object_umbrella": r"\bumbrella\b",
        "negation": r"\b(no|not|without|has no|doesn\'t|don\'t|isn\'t|lacks)\b|does not",
    },
    "house_left_of_tree": {
        "subject_house": r"\bhouse\b",
        "color_red": r"\bred\b",
        "color_blue": r"\bblue\b",
        "relation_left": r"\b(left|left side)\b",
        "subject_tree": r"\btree\b",
    },
    "woman_piano": {
        "subject_woman": r"\b(woman|lady|female)\b",
        "object_piano": r"\bpiano\b",
        "action_play": r"\b(play|playing|plays)\b",
    },
    "three_red_cats_bench": {
        "count_three": r"\b(three|3)\b",
        "subject_cat": r"\bcats?\b",
        "colors_red_blue": r"\bred\b.*\bblue\b|\bblue\b.*\bred\b",
        "seating_bench": r"\b(bench|seat)\b",
        "negation_hats": r"\b(without|no)\b[^.]*\b(hats?|caps?)\b",
    },
    "man_black_coat": {
        "subject_man": r"\bman\b",
        "garment_coat": r"\b(coat|jacket)\b",
        "garment_shirt": r"\bshirt\b",
        "color_black": r"\bblack\b",
        "color_white": r"\bwhite\b",
    },
    "night_city_stars": {
        "subject_city": r"\bcity\b",
        "time_night": r"\bnight|nighttime\b",
        "object_stars": r"\bstars?\b",
        "place_sky": r"\bsky\b",
    },
    "bird_on_branch": {
        "subject_bird": r"\bbird\b",
        "object_branch": r"\b(branch|branches|bough)\b",
        "action_perched": r"\b(stands?|standing|sits|sitting|perch(?:ed|ing)?)\b",
    },
    "boy_girl_hands": {
        "subject_boy": r"\bboy\b",
        "subject_girl": r"\bgirl\b",
        "object_hand": r"\bhand\b",
        "action_hold": r"\b(hold|holds|holding)\b",
    },
    "weighted_car_bridge": {
        "weight_syntax_kept": r"\(red car:1\.2\)",
        "subject_car": r"\bcar\b",
        "place_bridge": r"\bbridge\b",
        "negation_pedestrian": r"\b(no|without|not any)\b[^.]*\b(pedestrians?|passersby|people)\b",
    },
}


def evaluate(scene: str, english: str) -> dict[str, bool]:
    lowered = english.lower()
    return {name: bool(re.search(pattern, lowered)) for name, pattern in CHECKS[scene].items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", type=Path, default=Path("D:/AI/XiaoYe/qa/translation-matrix.json"))
    parser.add_argument(
        "--limit", type=int, default=0, help="只跑前 N 个场景（冒重用），0 = 全 120 条"
    )
    args = parser.parse_args()
    if not args.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("只接受回环地址：这一发要连着打上百次模型请求")

    client = OpenAiCompatClient(
        OpenAiCompatSettings(
            provider_name="local-translation-matrix",
            base_url=args.base_url,
            model=args.model,
            api_key_env="",
            timeout_seconds=180,
            max_retries=0,
            retry_backoff_seconds=0,
            key_optional=True,
        ),
        environ={},
    )
    translator = PromptTranslator(environ={}, client_provider=lambda: client)

    scenes = SCENES[: args.limit] if args.limit else SCENES
    rows: list[Row] = []
    for scene, texts in scenes:
        for language in LANGUAGES:
            source = texts[language]
            started = time.perf_counter()
            row = Row(scene=scene, language=language, source=source)
            try:
                result = translator.translate(source)
                row.checks = evaluate(scene, result.english)
                row.english = result.english
                row.provider = result.provider
                row.ok = all(row.checks.values())
            except Exception as exc:  # 一条失败不中断整轮，但必须落在结果里
                row.error = f"{type(exc).__name__}: {exc}"[:300]
            row.seconds = round(time.perf_counter() - started, 3)
            rows.append(row)
            print(json.dumps(asdict(row), ensure_ascii=False)[:400], flush=True)

    by_language = {
        language: sum(1 for row in rows if row.language == language and row.ok)
        for language in LANGUAGES
    }
    by_constraint: dict[str, dict[str, int]] = {}
    for name in sorted({key for checks in CHECKS.values() for key in checks}):
        scored = [row for row in rows if name in row.checks]
        by_constraint[name] = {
            "hit": sum(1 for row in scored if row.checks[name]),
            "total": len(scored),
        }

    passed = sum(1 for row in rows if row.ok)
    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "total": len(rows),
        "passed": passed,
        "pass_rate": round(passed / len(rows), 4) if rows else 0.0,
        "by_language": by_language,
        "by_constraint": by_constraint,
        "mean_seconds": round(sum(row.seconds for row in rows) / len(rows), 3) if rows else 0.0,
        "failures": [asdict(row) for row in rows if not row.ok],
        "cases": [asdict(row) for row in rows],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n合计 {passed}/{len(rows)} 全约束通过 = {summary['pass_rate'] * 100:.1f}%")
    print(
        "分语种（通过句数 / 每语种一句）：" + "  ".join(f"{k}={v}" for k, v in by_language.items())
    )
    print("分约束：")
    for name, value in by_constraint.items():
        print(f"  {name:22s} {value['hit']:3d}/{value['total']}")
    print(f"回执：{args.out}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

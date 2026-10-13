"""把中文那句"画什么"展开成 SD1.5 听得懂的英文标签。

**为什么需要这一层**：面板和对话两条路过去差得很远 —— 对话里模型会先把中文扩写成英文
标签再调 `generate_image`（r105 的工具描述硬性要求这么做），而面板把用户原话一个字不改
直接送进 `CLIPTextEncode`。SD1.5 的文本编码器只认英文标签，中文整句对它约等于噪声，
于是同一句「画一个绿色瘦弱的哥布林」在对话里画对了、在弹窗里画成了一位动漫美女。
把展开做在 :class:`jarvis.app.image_gen.ImageService` 的入口，两条路就共用同一个形状。

**为什么是词典而不是模型**：这一层必须离线、必须秒回、必须每次给同一个答案 ——
它要在用户按「画一张」的那一下生效，而不能等一个 130~300 秒的本地 4B，也不能在
没配云端钥匙时悄悄什么都不做。

**认不出来的词怎么说**：报出来，但**不进提示词**。r110 之前是"原样留下"，而"原样"
其实是把一句话拆成一个个汉字、用空格拼上去 —— SD1.5 的编码器一个汉字都不认，
那 21 个噪声字把 7 个真标签淹掉了。不吭声是骗人，吭了又塞进去是害人，两件事得分开做。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

_CJK: Final = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

_CJK_RUN: Final = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
"""没认出来的那一段汉字，**按段**取。

用 `_CJK.findall` 会把一句话拆成一个个单字，而 SD1.5 的文本编码器一个汉字都不认：
r110 实拍那次「在月光下翩翩起舞的猪头人」，提示词尾巴上挂了 21 个被空格隔开的汉字，
真的英文标签只有 7 个 —— 噪声占了四分之三，画出来就是一团认不出是什么的东西。
"""

# 量词、语气词、指令词，以及**中文特有的虚词**：它们不是画面内容，留着只会稀释注意力，
# 而且会让"没认出来的词"那一栏报出一堆"在、的、穿"这种没意义的话。
_NOISE: Final = (
    "帮我",
    "给我",
    "请你",
    "麻烦",
    "一个",
    "一张",
    "一只",
    "一位",
    "一台",
    "一幅",
    "张",
    "个",
    "只",
    "位",
    "的",
    "了",
    "吧",
    "呀",
    "呢",
    "要",
    "想",
    "请",
    "画",
    "生成",
    "做",
    "来",
    "整",
    "出",
    "这种",
    "那种",
    "这样",
    "那样",
    "一些",
    "有点",
    "比较",
    "很",
    "非常",
    "超",
    "更",
    # 虚词与方位：场景那一条已经把它翻成 "in a dark forest" 这种带前置词的短语，
    # 再留一个"在"字只会变成没认出来的噪声。
    "穿着",
    "戴着",
    "拿着",
    "正在",
    "可以",
    "一点",
    "带有",
    "和",
    "与",
    "跟",
    "或",
    "是",
    "有",
    "在",
    "穿",
    "戴",
    "拿",
    "着",
    "过",
    "把",
    "到",
    "从",
    "向",
    "对",
    "里",
    "内",
    "上",
    "下",
    "前",
    "后",
    "旁",
    "边",
    "中间",
    "旁边",
    "上面",
    "下面",
    "里面",
    "这样",
    "那样",
    "这样子的",
    "样子",
    "感觉",
    "风格",
    "风的",
    "一片",
    "一座",
    "一群",
    "一双",
    "一朵",
    "一条",
    "一",
    "座",
    "片",
    "朵",
    "条",
    "群",
    "双",
    "幅",
    "面",
    "扇",
    "即可",
    "就行",
)


# 这些词**要在词典那一趟里被吃掉，但不产出任何标签**。它们不能放进 `_NOISE`：
# 那一栏是在词典匹配**之后**才剥的，而"角色"里的"角"会先被单字条目吃成 "horns"、
# "背景"里的"景"什么都不剩却白占一次匹配。最长匹配先吃掉整词，才是这里要的顺序。
_SILENT: Final = (
    "角色",
    "背景",
    "周围",
    "点缀",
    "画面",
    "整体",
    "感觉",
    "一些",
    "这样",
    "那样",
    "以及",
    "还有",
    "呈现",
    "带有",
    "充满",
    "略微",
    "些许",
)


# 分组顺序就是拼装的顺序：主体 → 属性 → 服饰 → 场景 → 光照 → 构图 → 风格 → 动作。
# 这个顺序是照 r105 工具描述里那句"主体 → 关键属性 → 场景 → 光照 → 风格"来的，
# 而那条顺序是对话里真画对过绿色哥布林的那一次用的形状。
GROUPS: Final[dict[str, dict[str, str]]] = {
    "subject": {
        "哥布林": "goblin",
        "地精": "goblin",
        "兽人": "orc",
        "半兽人": "half-orc",
        "精灵": "elf",
        "矮人": "dwarf",
        "巨人": "giant",
        "泰坦": "titan",
        "恶魔": "demon",
        "魔鬼": "devil",
        "天使": "angel",
        "死神": "grim reaper",
        "骷髅": "skeleton",
        "僵尸": "zombie",
        "丧尸": "zombie",
        "幽灵": "ghost",
        "鬼": "spirit",
        "吸血鬼": "vampire",
        "狼人": "werewolf",
        "龙": "dragon",
        "幼龙": "young dragon",
        "凤凰": "phoenix",
        "神兽": "divine beast",
        "麒麟": "qilin",
        "独角兽": "unicorn",
        "美人鱼": "mermaid",
        "巫师": "wizard",
        "法师": "mage",
        "术士": "warlock",
        "女巫": "witch",
        "骑士": "knight",
        "战士": "warrior",
        "弓箭手": "archer",
        "刺客": "assassin",
        "盗贼": "thief",
        "牧师": "priest",
        "僧人": "monk",
        "武士": "samurai",
        "忍者": "ninja",
        "国王": "king",
        "女王": "queen",
        "王子": "prince",
        "公主": "princess",
        "皇帝": "emperor",
        "将军": "general",
        "商人": "merchant",
        "农夫": "farmer",
        "铁匠": "blacksmith",
        "猎人": "hunter",
        "渔夫": "fisherman",
        "水手": "sailor",
        "学生": "student",
        "老师": "teacher",
        "医生": "doctor",
        "护士": "nurse",
        "警察": "police officer",
        "消防员": "firefighter",
        "宇航员": "astronaut",
        "机器人": "robot",
        # 「猪头人」这类"某动物的头 + 人"是用户真会说的话（r110 实拍）。
        # r110 量的是三个标签 vs 自造的 "pig-headed humanoid"（4 个种子：3/4 vs 0/4，
        # 所以那时写成三个标签是对的）。r112 又量了一轮，只动主体这一截、其余一个字不改：
        #   `pig head, anthropomorphic pig, humanoid`（不加权）  2/4   ← r110 那句原样重测
        #   同上 + `solo`                                        3/4
        #   同上 + `fully clothed`                               2/4（顺带量出，颜色零效果）
        #   去掉 `pink skin` 后的三标签                            2/4
        #   去掉 `pink skin` + `solo`                            1/4
        #   三标签去掉 `anthropomorphic pig`（不加权）             0/4   ← 失败的是"兽耳少女"
        #   三标签去掉 `anthropomorphic pig` + `(pig head:1.4)`   **4/4**
        #   加权重但保留 `anthropomorphic pig`                    3/4
        # 所以留下来的形状是**加权的一标签**：4/4 的那一串就是线上真正跑的那一串，
        # 不多一个词。`solo`／`fully clothed` 不加 —— 猪头那 3/4→1/4 是噪声（N=4 下
        # 一张翻面说明不了什么），而颜色上它们和对照组分毫不差（5x% → 5x%）。
        "猪头人": "(pig head:1.4), humanoid",
        # 下面这四个**没量过**，保持 r110 的三标签形状：r110 那次量到"换狼 4/4 都对"。
        # 加权重那一轮只跑了猪这一句（4 个种子、同一句、其余一个字不改），照搬到别的
        # 动物上就是猜 —— 要动它们，先照上面那张表的样子自己跑一轮 N=4。
        "马头人": "horse head, anthropomorphic horse, humanoid",
        "狼头人": "wolf head, anthropomorphic wolf, humanoid",
        "鸟头人": "bird head, anthropomorphic bird, humanoid",
        "猫头人": "cat head, anthropomorphic cat, humanoid",
        "人形": "humanoid",
        "兽人形态": "anthropomorphic beast",
        "机甲": "mech",
        "机械": "machinery",
        "人工智能": "artificial intelligence",
        "男人": "man",
        "女人": "woman",
        "男孩": "boy",
        "女孩": "girl",
        "少年": "teenager",
        "少女": "young girl",
        "老人": "old person",
        "老爷爷": "old man",
        "老奶奶": "old woman",
        "婴儿": "baby",
        "猫": "cat",
        "小猫": "kitten",
        "狗": "dog",
        "小狗": "puppy",
        "狼": "wolf",
        "狐狸": "fox",
        "兔子": "rabbit",
        "熊": "bear",
        "熊猫": "giant panda",
        "老虎": "tiger",
        "狮子": "lion",
        "豹": "leopard",
        "马": "horse",
        "白马": "white horse",
        "鹿": "deer",
        "牛": "cow",
        "羊": "sheep",
        "猪": "pig",
        "鸡": "chicken",
        "鸭": "duck",
        "鹅": "goose",
        "鸟": "bird",
        "鹰": "eagle",
        "猫头鹰": "owl",
        "乌鸦": "crow",
        "企鹅": "penguin",
        "鱼": "fish",
        "金鱼": "goldfish",
        "鲨鱼": "shark",
        "鲸鱼": "whale",
        "海豚": "dolphin",
        "章鱼": "octopus",
        "水母": "jellyfish",
        "螃蟹": "crab",
        "龙虾": "lobster",
        "虾": "shrimp",
        "蝴蝶": "butterfly",
        "蜜蜂": "bee",
        "蚂蚁": "ant",
        "蜘蛛": "spider",
        "蜻蜓": "dragonfly",
        "甲虫": "beetle",
        "蛇": "snake",
        "蜥蜴": "lizard",
        "青蛙": "frog",
        "乌龟": "turtle",
        "鳄鱼": "crocodile",
        "恐龙": "dinosaur",
        "树": "tree",
        "松树": "pine tree",
        "柳树": "willow",
        "樱花": "cherry blossom",
        "玫瑰": "rose",
        "莲花": "lotus",
        "向日葵": "sunflower",
        "郁金香": "tulip",
        "菊花": "chrysanthemum",
        "仙人掌": "cactus",
        "蘑菇": "mushroom",
        "草": "grass",
        "花": "flower",
        "山": "mountain",
        "河": "river",
        "湖": "lake",
        "海": "sea",
        "瀑布": "waterfall",
        "岛": "island",
        "城堡": "castle",
        "塔": "tower",
        "桥": "bridge",
        "房子": "house",
        "小屋": "cottage",
        "村庄": "village",
        "城市": "city",
        "街道": "street",
        "巷子": "alley",
        "屋顶": "rooftop",
        "房间": "room",
        "卧室": "bedroom",
        "客厅": "living room",
        "厨房": "kitchen",
        "教室": "classroom",
        "图书馆": "library",
        "咖啡馆": "cafe",
        "酒吧": "bar",
        "商店": "shop",
        "市场": "market",
        "车站": "station",
        "机场": "airport",
        "码头": "dock",
        "工厂": "factory",
        "废墟": "ruins",
        "墓地": "graveyard",
        "神庙": "temple",
        "教堂": "church",
        "道观": "taoist temple",
        "花园": "garden",
        "公园": "park",
        "森林": "forest",
        "竹林": "bamboo forest",
        "沙漠": "desert",
        "草原": "grassland",
        "雪山": "snowy mountain",
        "冰川": "glacier",
        "峡谷": "canyon",
        "洞穴": "cave",
        "火山": "volcano",
        "田野": "field",
        "农田": "farmland",
        "铁路": "railway",
        "公路": "highway",
        "汽车": "car",
        "跑车": "sports car",
        "卡车": "truck",
        "巴士": "bus",
        "摩托": "motorcycle",
        "自行车": "bicycle",
        "火车": "train",
        "高铁": "bullet train",
        "飞机": "airplane",
        "直升机": "helicopter",
        "船": "ship",
        "帆船": "sailboat",
        "潜艇": "submarine",
        "坦克": "tank",
        "宇宙飞船": "spaceship",
        "火箭": "rocket",
        "卫星": "satellite",
        "空间站": "space station",
        "剑": "sword",
        "刀": "blade",
        "斧": "axe",
        "锤": "hammer",
        "盾": "shield",
        "弓": "bow",
        "枪": "gun",
        "法杖": "staff",
        "书": "book",
        "地图": "map",
        "宝箱": "treasure chest",
        "灯": "lamp",
        "蜡烛": "candle",
        "镜子": "mirror",
        "伞": "umbrella",
        "面具": "mask",
        "王冠": "crown",
        "戒指": "ring",
        "项链": "necklace",
        "茶": "tea",
        "咖啡": "coffee",
        "酒": "wine",
        "蛋糕": "cake",
        "面包": "bread",
        "米饭": "rice",
        "面条": "noodles",
        "水果": "fruit",
        "苹果": "apple",
        "橙": "orange",
        "香蕉": "banana",
        "葡萄": "grapes",
        "西瓜": "watermelon",
        "草莓": "strawberry",
        "食物": "food",
        "猫耳": "cat ears",
        "翅膀": "wings",
        "尾巴": "tail",
        "角": "horns",
        "爪子": "claws",
    },
    "attribute": {
        "绿色": "green",
        "红色": "red",
        "橙色": "orange",
        "黄色": "yellow",
        "蓝色": "blue",
        "紫色": "purple",
        "粉色": "pink",
        "粉红色": "pink",
        "黑色": "black",
        "白色": "white",
        "灰色": "gray",
        "金色": "golden",
        "银色": "silver",
        "棕色": "brown",
        "褐色": "brown",
        "青色": "cyan",
        "透明": "translucent",
        "彩色": "colorful",
        "发光": "glowing",
        "瘦弱": "thin",
        "瘦": "thin",
        "强壮": "strong",
        "肌肉": "muscular",
        "高大": "tall",
        "高": "tall",
        "矮": "short",
        "矮小": "short",
        "肥胖": "plump",
        "胖": "plump",
        "年轻": "young",
        "年老": "elderly",
        "古老": "ancient",
        "可爱": "cute",
        "萌": "adorable",
        "帅气": "handsome",
        "美丽": "beautiful",
        "漂亮": "pretty",
        "优雅": "elegant",
        "凶猛": "fierce",
        "凶恶": "menacing",
        "威严": "majestic",
        "温柔": "gentle",
        "忧郁": "melancholic",
        "神秘": "mysterious",
        "破旧": "worn-out",
        "华丽": "ornate",
        "简单": "simple",
        "复杂": "intricate",
        "巨大": "gigantic",
        "微小": "tiny",
        "尖耳朵": "pointed ears",
        "尖耳": "pointed ears",
        "长发": "long hair",
        "短发": "short hair",
        "白发": "white hair",
        "黑发": "black hair",
        "金发": "blonde hair",
        "红发": "red hair",
        "蓝发": "blue hair",
        "紫发": "purple hair",
        "绿发": "green hair",
        "双马尾": "twin tails",
        "眼睛": "eyes",
        "独眼": "one-eyed",
        "伤疤": "scar",
        "纹身": "tattoo",
        "机械臂": "mechanical arm",
        "破损": "damaged",
        "干净": "clean",
        "脏": "dirty",
        "湿": "wet",
        "干": "dry",
        "冷": "cold",
        "热": "hot",
        "冰": "icy",
        "火": "fiery",
        "电": "electric",
        "毒": "poisonous",
        "神圣": "holy",
        "邪恶": "evil",
        # 单字颜色：用户真的会写"绿哥布林""白猫"。最长匹配先吃掉"绿色"，
        # 所以这些单字只在没写"色"的时候才命中。
        "红": "red",
        "橙": "orange",
        "黄": "yellow",
        "绿": "green",
        "青": "cyan",
        "蓝": "blue",
        "紫": "purple",
        "粉": "pink",
        "黑": "black",
        "白": "white",
        "灰": "gray",
        "金": "golden",
        "银": "silver",
        "棕": "brown",
        "褐": "brown",
        "橘": "orange",
        "彩": "colorful",
    },
    "clothing": {
        "盔甲": "armor",
        "铠甲": "plate armor",
        "皮甲": "leather armor",
        "斗篷": "cape",
        "披风": "cloak",
        "长袍": "robe",
        "袍子": "robe",
        "衣服": "clothes",
        "衬衫": "shirt",
        "外套": "jacket",
        "大衣": "coat",
        "西装": "suit",
        "裙子": "skirt",
        "连衣裙": "dress",
        "和服": "kimono",
        "汉服": "hanfu",
        "旗袍": "qipao",
        "制服": "uniform",
        "校服": "school uniform",
        "盔甲裙": "armored dress",
        "帽子": "hat",
        "兜帽": "hood",
        "头盔": "helmet",
        "头巾": "headscarf",
        "眼镜": "glasses",
        "墨镜": "sunglasses",
        "靴子": "boots",
        "鞋子": "shoes",
        "手套": "gloves",
        "围巾": "scarf",
        "腰带": "belt",
        "头饰": "hair ornament",
        "发饰": "hair ornament",
        "背包": "backpack",
        "口袋": "pocket",
        "裸": "bare",
        "赤脚": "barefoot",
    },
    "scene": {
        "森林": "in a dark forest",
        "树林": "among trees",
        "山洞": "inside a cave",
        "洞穴": "inside a cave",
        "城堡": "in front of a castle",
        "废墟": "among ruins",
        "墓地": "in a graveyard",
        "村庄": "in a village",
        "城市": "in a city",
        "街道": "on a street",
        "屋顶": "on a rooftop",
        "教室": "in a classroom",
        "图书馆": "in a library",
        "卧室": "in a bedroom",
        "厨房": "in a kitchen",
        "花园": "in a garden",
        "公园": "in a park",
        "沙漠": "in a desert",
        "雪山": "on a snowy mountain",
        "海边": "by the sea",
        "湖边": "by a lake",
        "河边": "by a river",
        "瀑布": "beside a waterfall",
        "峡谷": "in a canyon",
        "战场": "on a battlefield",
        "火山": "near a volcano",
        "太空": "in outer space",
        "宇宙": "in outer space",
        "月球": "on the moon",
        "火星": "on mars",
        "水下": "underwater",
        "空中": "in the sky",
        "云端": "among the clouds",
        "雨夜": "on a rainy night",
        "雪夜": "on a snowy night",
        "夜晚": "at night",
        "白天": "in daytime",
        "黄昏": "at dusk",
        "黎明": "at dawn",
        "清晨": "in the early morning",
        "傍晚": "in the evening",
        "春天": "in spring",
        "夏天": "in summer",
        "秋天": "in autumn",
        "冬天": "in winter",
        "雪": "snowing",
        "雨": "raining",
        "雾": "foggy",
        "薄雾": "misty",
        "湖泊": "lake",
        "小溪": "stream",
        "草地": "meadow",
        "云": "cloudy",
        "彩虹": "rainbow",
        "月亮": "moon",
        "满月": "full moon",
        "太阳": "sun",
        "星星": "stars",
        "星空": "starry sky",
        "闪电": "lightning",
        "篝火": "bonfire",
        "火堆": "campfire",
        "火焰": "flames",
    },
    "light": {
        "月光": "moonlight",
        "阳光": "sunlight",
        "日光": "daylight",
        "逆光": "backlight",
        "侧光": "side lighting",
        "顶光": "top lighting",
        "霓虹": "neon lights",
        "烛光": "candlelight",
        "火光": "firelight",
        "灯光": "lamplight",
        "柔光": "soft lighting",
        "温暖": "warm lighting",
        "暖色": "warm colors",
        "强光": "harsh lighting",
        "暗光": "dim lighting",
        "剪影": "silhouette",
        "体积光": "volumetric lighting",
        "光晕": "lens flare",
        "阴影": "dramatic shadows",
        "反光": "reflective",
        "通透": "translucent lighting",
    },
    "framing": {
        "特写": "close-up",
        "大头": "portrait shot",
        "半身": "upper body",
        "半身像": "bust portrait",
        "全身": "full body",
        "远景": "wide shot",
        "鸟瞰": "bird's-eye view",
        "俯视": "top down view",
        "仰视": "low angle view",
        "侧面": "side view",
        "背影": "from behind",
        "对称": "symmetrical",
        "居中": "centered composition",
        "留白": "negative space",
        "群像": "group shot",
        "第一人称": "first person view",
    },
    "action": {
        "站立": "standing",
        "起舞": "dancing",
        "翩翩起舞": "dancing",
        "舞动": "dancing",
        "旋转": "spinning",
        "坐着": "sitting",
        "蹲": "crouching",
        "跪": "kneeling",
        "躺": "lying down",
        "奔跑": "running",
        "走": "walking",
        "跳": "jumping",
        "飞": "flying",
        "游泳": "swimming",
        "战斗": "in combat",
        "挥剑": "swinging a sword",
        "拉弓": "drawing a bow",
        "施法": "casting a spell",
        "喷火": "breathing fire",
        "举": "holding up",
        "抱": "holding",
        "阅读": "reading",
        "写作": "writing",
        "睡觉": "sleeping",
        "吃饭": "eating",
        "喝茶": "drinking tea",
        "抽": "smoking",
        "弹吉他": "playing guitar",
        "弹钢琴": "playing piano",
        "唱歌": "singing",
        "跳舞": "dancing",
        "微笑": "smiling",
        "笑": "laughing",
        "哭": "crying",
        "怒吼": "roaring",
        "凝视": "staring",
        "回头看": "looking back",
        "思考": "thinking",
        "工作": "working",
        "训练": "training",
        "休息": "resting",
    },
    "style": {
        "写实": "photorealistic",
        "真实": "realistic",
        "照片": "photography",
        "摄影": "photography",
        "插画": "illustration",
        "动漫": "anime style",
        "二次元": "anime style",
        "卡通": "cartoon style",
        "油画": "oil painting",
        "水彩": "watercolor",
        "水墨": "ink wash painting",
        "国画": "traditional chinese painting",
        "素描": "pencil sketch",
        "线稿": "line art",
        "上色": "colored",
        "像素": "pixel art",
        "赛博朋克": "cyberpunk",
        "蒸汽朋克": "steampunk",
        "奇幻": "fantasy",
        "童话": "fairytale",
        "科幻": "sci-fi",
        "恐怖": "horror",
        "黑暗": "dark fantasy",
        "治愈": "healing atmosphere",
        "梦幻": "dreamlike",
        "国风": "chinese style",
        "古风": "ancient chinese style",
        "日式": "japanese style",
        "欧式": "european style",
        "中世纪": "medieval",
        "未来": "futuristic",
        "废土": "post-apocalyptic",
        "3d": "3d render",
        "三维": "3d render",
        "概念图": "concept art",
        "厚涂": "impasto",
        "扁平": "flat design",
        "极简": "minimalist",
        "哥特": "gothic",
        "巴洛克": "baroque",
        "复古": "retro",
        "电影感": "cinematic",
        "海报": "poster design",
        "封面": "cover art",
        "壁纸": "wallpaper",
        "头像": "avatar portrait",
        # "插画风""动漫风"这类"…风"后缀：不补进来就会剩一个"风"字被当成没认出来的词，
        # 更糟的是它可能被读成 windy。最长匹配保证"插画风"先于"插画"被吃掉。
        "插画风": "illustration",
        "动漫风": "anime style",
        "二次元风": "anime style",
        "卡通风": "cartoon style",
        "写实风": "photorealistic",
        "水彩风": "watercolor",
        "油画风": "oil painting",
        "水墨风": "ink wash painting",
        "素描风": "pencil sketch",
        "像素风": "pixel art",
        "赛博风": "cyberpunk",
        "奇幻风": "fantasy",
        "科幻风": "sci-fi",
        "恐怖风": "horror",
        "治愈风": "healing atmosphere",
        "可爱风": "cute style",
        "复古风": "retro",
        "极简风": "minimalist",
        "精致": "highly detailed",
        "细节": "intricate details",
        "8k": "8k",
        "4k": "4k",
    },
}

# 颜色写在名词前面之外，SD1.5 还吃"重复一次"这一套：r105 对话里画对的那一次，
# 模型自己就是这么写的（green goblin, green skin）。这里只在**有主体**时补一次，
# 没有主体就只留颜色本身，不凭空造一个 "skin"。

# 会动的主体：颜色要写在它前面（green goblin），"再说一次颜色"也只对它做
# （green skin）—— 对一座山做这两件事没有意义。
_BEINGS: Final[frozenset[str]] = frozenset(
    {
        "goblin",
        "orc",
        "half-orc",
        "elf",
        "dwarf",
        "giant",
        "titan",
        "demon",
        "devil",
        "angel",
        "grim reaper",
        "skeleton",
        "zombie",
        "ghost",
        "spirit",
        "vampire",
        "werewolf",
        "dragon",
        "young dragon",
        "phoenix",
        "qilin",
        "unicorn",
        "mermaid",
        "wizard",
        "mage",
        "warlock",
        "witch",
        "knight",
        "warrior",
        "archer",
        "assassin",
        "thief",
        "priest",
        "monk",
        "samurai",
        "ninja",
        "king",
        "queen",
        "prince",
        "princess",
        "emperor",
        "general",
        "merchant",
        "farmer",
        "blacksmith",
        "hunter",
        "fisherman",
        "sailor",
        "student",
        "teacher",
        "doctor",
        "nurse",
        "police officer",
        "firefighter",
        "astronaut",
        "robot",
        "mech",
        "man",
        "woman",
        "boy",
        "girl",
        "teenager",
        "young girl",
        "old person",
        "old man",
        "old woman",
        "baby",
        "cat",
        "kitten",
        "dog",
        "puppy",
        "wolf",
        "fox",
        "rabbit",
        "bear",
        "giant panda",
        "tiger",
        "lion",
        "leopard",
        "horse",
        "white horse",
        "deer",
        "cow",
        "sheep",
        "pig",
        "chicken",
        "duck",
        "goose",
        "bird",
        "eagle",
        "owl",
        "crow",
        "penguin",
        "fish",
        "goldfish",
        "shark",
        "whale",
        "dolphin",
        "octopus",
        "jellyfish",
        "crab",
        "lobster",
        "shrimp",
        "butterfly",
        "bee",
        "ant",
        "spider",
        "dragonfly",
        "beetle",
        "snake",
        "lizard",
        "frog",
        "turtle",
        "crocodile",
        "dinosaur",
    }
)

_COLORS: Final[frozenset[str]] = frozenset(
    {
        "green",
        "red",
        "orange",
        "yellow",
        "blue",
        "purple",
        "pink",
        "black",
        "white",
        "gray",
        "golden",
        "silver",
        "brown",
        "cyan",
        "translucent",
        "colorful",
    }
)

# 这些动作发生在**全身**上：只写 "dancing" 而不给构图位，模型就把人裁到腰，
# 舞步整个看不见。r110 实测（同一句、4 个种子）：不带构图词 0/4 画出"兽头人身在跳"，
# 带上 "full body, dynamic pose" 3/4 画出来了 —— 所以这不是替用户编风格，
# 是把她已经说出口的那个动作画得出来。用户自己点了构图（特写/半身…）就不补。
_WHOLE_BODY_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "dancing",
        "running",
        "jumping",
        "flying",
        "swimming",
        "walking",
        "crouching",
        "kneeling",
        "lying down",
        "in combat",
        "swinging a sword",
        "drawing a bow",
        "casting a spell",
        "spinning",
        "training",
        "playing guitar",
        "playing piano",
    }
)

# 拼装顺序 = 递给引擎的顺序。主体在前是 SD1.5 唯一稳定吃得住的位置。
_GROUP_ORDER: Final[tuple[str, ...]] = (
    "subject",
    "attribute",
    "clothing",
    "object",
    "scene",
    "light",
    "framing",
    "action",
    "style",
)


@dataclass(frozen=True)
class Expansion:
    """一次展开的结果。`unknown` 是**必须让用户看见**的那一半。"""

    prompt: str
    tags: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    """词典认出来的**主体**（`GROUPS["subject"]` 命中的那些标签）。

    `merge` 要靠它分胜负：词典认出主体，就说明这一句最难的那半它有现成答案
    （「猪头人」→ `pig head, anthropomorphic pig, humanoid`，这几个标签是对着
    anythingV3 调过的），这时候模型对同一个主体的猜测只会把画面带偏。
    """

    @property
    def changed(self) -> bool:
        return bool(self.tags)

    @property
    def note(self) -> str:
        """给人看的那句补充：哪些词词典没覆盖到。"""
        if not self.unknown:
            return ""
        return "这几个词没翻出来，所以没有进提示词（引擎只认英文标签）：" + "、".join(self.unknown)


def english_only(text: str) -> str:
    """把"英文标签 + 汉字残渣"里的汉字整段抹掉，英文和标点一个不碰。

    `zh` 在的时候，`prompt` 里那句是**模型自己写的英文标签串**，里面的汉字是它把用户
    原话拆碎之后漏下来的残渣。r112 实拍那串里，残渣中的「头 饰」「温 暖」「童 话」被
    词典当正经词吃成了 ``hair ornament`` / ``warm lighting`` / ``fairytale`` ——
    拆碎的噪声变成了三个像模像样的标签，**比它原样当噪声递进去更隐蔽**；
    其中 ``warm lighting`` 还和用户说的 ``moonlight`` 打架。所以走这条路时先剥汉字再展开：
    模型写了什么英文就算什么，汉字一个字都算不了数。
    """
    return _CJK.sub(" ", text or "")


def expand_model(text: str) -> Expansion:
    """模型写的那串英文标签：**先抹掉汉字残渣再展开**。

    单独做成一个函数，是因为这条规矩必须只有一个出口。r112 的探针脚本一开始直接调
    ``expand(模型那串)``，于是它印出来的合并结果和真正进引擎的那句不一样 ——
    量出来的东西和线上跑的不是同一件事，比量错还难发现。
    """
    return expand(english_only(text))


def has_chinese(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def _entries() -> list[tuple[str, str, str]]:
    """(中文词, 英文标签, 分组)，按中文词长度从长到短 —— 长词先吃，"插画风"才不会剩个"风"。

    `_SILENT` 那一组带着空标签进来：它们照样参与"最长先吃"，只是不产出英文。
    """
    rows: list[tuple[str, str, str]] = [(word, "", "silent") for word in _SILENT]
    for group, table in GROUPS.items():
        for zh, en in table.items():
            rows.append((zh, en, group))
    rows.sort(key=lambda row: len(row[0]), reverse=True)
    return rows


_TABLE: Final[tuple[tuple[str, str, str], ...]] = tuple(_entries())


def _strip_noise(text: str) -> str:
    for word in sorted(_NOISE, key=len, reverse=True):
        text = text.replace(word, " ")
    return text


_CJK_GAP: Final = re.compile(r"(?<=[㐀-䶿一-鿿])\s+(?=[㐀-䶿一-鿿])")
"""两个汉字之间的空白。中文不用空格分词，所以**接回去是无损的**。

为什么要有这一条：对话那条路进来的不是人打的字，是模型吐的标签串，而模型把没翻出来的词
拆成了一个个单字用空格拼着（r112 实拍：`头 人 起 舞 头 饰 随 舞 动 …`）。不接回去，
词典按整词匹配的"起舞"永远匹配不上一个写成"起 舞"的东西 —— 明明认得的词被空格挡在外面。
"""


def _join_cjk(text: str) -> str:
    """把被空格隔开的汉字接回成词。只动汉字之间的空白，英文词之间的空格一个不碰。"""
    return _CJK_GAP.sub("", text)


def _english_pieces(text: str) -> list[str]:
    """从"英文标签 + 汉字残渣"里把英文那些段取出来，按逗号切。

    调用方写的英文**必须留下**：对话那条路的 `prompt` 整句都是模型写的英文标签，
    r112 之前只要句尾挂着一个认得的中文词，这里就只回词典那几个标签、把模型写的
    七个标签整串扔掉 —— 那是在拿词典的部分覆盖面去换掉调用方的全部意图。
    """
    out: list[str] = []
    for piece in text.split(","):
        cleaned = re.sub(r"\s+", " ", _CJK_RUN.sub(" ", piece)).strip(" -_|/")
        if cleaned and not _CJK.search(cleaned):
            out.append(cleaned)
    return out


def _dedupe(tags: list[str]) -> list[str]:
    """去掉被别的标签整段包住的短标签：有 "in a dark forest" 就不用再来一个 "forest"。"""
    out: list[str] = []
    for tag in tags:
        if any(tag != other and tag in other.split() for other in tags):
            continue
        if tag not in out:
            out.append(tag)
    return out


def expand(text: str) -> Expansion:
    """中文 → 英文标签串。

    **没有中文就原样返回** —— 模型已经扩写过的那条路（对话里）必须一个字都不动，
    否则这一层会去改一句本来正确的英文标签，而它比模型更不懂用户想要什么。
    """
    raw = str(text or "").strip()
    if not raw or not has_chinese(raw):
        return Expansion(prompt=raw)
    # 先把被空格拆散的汉字接回成词（模型吐的串长这样），再按词典最长匹配，**最后**剥虚词：
    # 反过来会把"插画"这种词从中间劈开。
    rest = _join_cjk(raw)
    found: dict[str, list[str]] = {group: [] for group in GROUPS}
    for zh, en, group in _TABLE:
        if zh in rest:
            rest = rest.replace(zh, " ")
            # 值里带 ", " 就是"一个词翻成一串标签"（兽头人那一类要的就是这个）。
            for piece in ([] if not en else en.split(", ")):
                if piece not in found[group]:
                    found[group].append(piece)
    # 剩下的按**段**报，且一段都不进提示词：SD1.5 的编码器不认汉字，
    # 把它们拼上去只是拿噪声把真标签淹掉（r110 实拍那次就是四分之三的 token 是汉字）。
    unknown = tuple(run for run in _CJK_RUN.findall(_strip_noise(rest)) if run.strip())
    # 调用方自己写的英文留着 —— 它和"没认出来的汉字"混在同一个 rest 里，按逗号切开取非汉字那些段。
    kept = _english_pieces(rest)

    subjects = found.get("subject", [])
    beings = [tag for tag in subjects if tag in _BEINGS]
    colors = [tag for tag in found.get("attribute", []) if tag in _COLORS]
    others = [tag for tag in found.get("attribute", []) if tag not in _COLORS]

    # 主体：优先"会动的"那一个；没有生物就用第一个物品（红色的跑车也要有名词可贴）。
    things = [tag for tag in subjects if tag not in _BEINGS]
    lead = beings[0] if beings else (things[0] if things else "")
    living = lead in _BEINGS

    head: list[str] = []
    if lead and (colors or others):
        head.extend(f"{color} {lead}" for color in colors)
        # 皮肤那一层只对会动的东西说：给一座山写 "green skin" 是噪声。
        if living:
            head.extend(f"{color} skin" for color in colors)
        # **属性带着主体再说一遍**（thin goblin）：这是中心 0.52/外圈 0.17 那一组的形状。
        # 只重复前三个属性 —— 每个都重复会把名词本身淹掉，而淹掉之后谁都不占权重。
        head.extend(f"{attr} {lead}" for attr in others[:3])
        head.extend(others[3:])
        head.extend(beings[1:] if lead in beings else beings)
    else:
        head.extend(beings)
        head.extend(colors)
        head.extend(others)
    actions = set(found.get("action", []))
    if actions & _WHOLE_BODY_ACTIONS and not found.get("framing"):
        found["framing"] = ["full body", "dynamic pose"]

    tags = _dedupe(
        head
        + found.get("clothing", [])
        + things
        + found.get("scene", [])
        + found.get("light", [])
        + found.get("framing", [])
        + found.get("action", [])
        + found.get("style", [])
    )
    # 调用方的英文排在前面：SD1.5 吃前面的 token 更重，而那串里带着的是它自己选的主体。
    # 词典捡回来的（这一句里是 dancing + 构图）接在后面补，不去抢主体的位置。
    merged = _dedupe(kept + tags)
    # **空串也要如实回空串**：一句话一个词都没认出来时，r112 之前这里回的是 `raw`，
    # 于是 21 个汉字原样进了 CLIPTextEncode —— 那正好违反这个模块开头立的规矩
    # （"认不出来的词：报出来，但不进提示词"）。回空串让上面那层说"我一个词都没认出来"，
    # 比画一团噪声再管它叫成功要诚实。
    return Expansion(
        prompt=", ".join(merged),
        tags=tuple(tags),
        unknown=unknown,
        subjects=tuple(subjects),
    )


_SUBJECT_STOPWORDS: Final = frozenset({"a", "an", "the", "of", "in", "on", "with", "and"})
"""判"是不是在说同一个主体"时不算数的词。没有它们，``a cat`` 和 ``a bonfire`` 会因为
共用一个 ``a`` 被判成同一个主体，于是模型那串被整份误杀。"""


_BODY_WORDS: Final[frozenset[str]] = frozenset(
    {"skin", "fur", "hair", "scales", "feathers", "face", "body"}
)
"""会说颜色的那一层身体词 —— 也就是词典自己占着的那个位置。

`expand` 给"会动的主体"说颜色时用的正是 ``{color} skin``（见那里 `if living` 那一段），
所以 ``pink skin`` 这种形状就是**词典的色位**，不是模型的自由发挥。"""


def _words(tag: str) -> frozenset[str]:
    return frozenset(word for word in re.split(r"[^a-z0-9]+", tag.lower()) if word)


def _named_a_colour(expansion: Expansion) -> bool:
    """用户那句话里有没有颜色。判标签而不是判原话：原话已经过完词典了。"""
    return any(_COLORS & _words(tag) for tag in expansion.tags)


def _subject_words(subjects: Iterable[str]) -> frozenset[str]:
    """词典认出的主体里那些**能用来认人的词**。

    ``pig head, anthropomorphic pig, humanoid`` → ``{pig, head, anthropomorphic, humanoid}``。
    长度 1-2 的词和 :data:`_SUBJECT_STOPWORDS` 不算 —— 它们太容易在别的标签里撞上。
    """
    out: set[str] = set()
    for tag in subjects:
        for word in re.split(r"[^a-z0-9]+", tag.lower()):
            if len(word) > 2 and word not in _SUBJECT_STOPWORDS:
                out.add(word)
    return frozenset(out)


def merge(spoken: Expansion, written: Expansion) -> Expansion:
    """用户原话的展开（`spoken`）和模型写的那串英文（`written`）合成一个。

    **主体归词典，其余归模型。**

    为什么要分这么细，而不是"认出主体就整份用词典那份"：两种情况都是真的，一刀切会各错一半。

    * r112 实拍「在月光下翩翩起舞的猪头人」。模型写的是
      ``pink pig, pink skin, glowing pig, fiery pig``——**一头粉色的猪**，而且把「翩翩起舞」
      整句丢了。词典给的是 ``pig head, anthropomorphic pig, humanoid``（对着 anythingV3 调过的）。
      两串直接拼在一起，四个"猪"的说法压过三个"猪头人"的说法，画出来还是一头猪。
      **模型那几个抢主体的标签必须死。**
    * 可是「画一台赛博朋克风格的摩托车」里，模型写的 ``neon lights, rain, night city``
      比词典那两个标签丰富得多，而且主体（摩托车）它根本没搞错。
      **整份丢掉就是把能画的变成画不了。**

    所以分的那条线是"**有没有在抢同一个主体**"：模型的标签里只要出现了词典主体中的某个词
    （:func:`_subject_words`），就算在说同一样东西 —— 换成词典的说法，模型那条丢掉；
    撞不上的（光照、场景、风格）一条不动，排在词典标签后面。

    主体身上的颜色也算主体的：`pink skin` 不含主体词，可它是模块把「猪」读成「粉猪」时
    顺手写下来的同一层东西（用户原话里一个颜色都没有）。r112 量过，同一个种子 4/4 张：
    带 `pink skin` 的那组粉色像素占 56.3%、平均饱和 0.325，去掉之后 35.8%、0.209 ——
    而用户报的原话就是「粉色的一团」。词典自己给颜色时用的形状也是 `{color} skin`，
    所以这个位置本来就归词典。用户真点了颜色（绿色的哥布林）时这一条不开火。

    词典连主体都没认出来时，模型那串当主干，词典只把它认得的那些补上去；
    词典一个词都没认出来时原封不动用模型那串（不许退化成空提示词）。
    """
    mine = list(spoken.tags)
    # 按逗号切 `written.prompt`，**不要用 `written.tags`**：`tags` 里只有词典命中的那些，
    # 模型自己写的英文不在里面。模型那串里混进一两个汉字残渣时（它真的会），
    # `tags` 就非空了，于是模型的英文被整份静默丢掉 —— 这正是 `expand` 里刚修掉的那个坑。
    theirs = [piece.strip() for piece in written.prompt.split(",") if piece.strip()]
    if not mine:
        return written
    if spoken.subjects:
        claimed = _subject_words(spoken.subjects)
        # 用户一个颜色都没说时，主体身上的颜色也归词典 —— 也就是"空着"。
        colour_slot_is_mine = not _named_a_colour(spoken)

        def steals_the_subject(tag: str) -> bool:
            words = _words(tag)
            if claimed & {word for word in words if len(word) > 2}:
                return True
            return colour_slot_is_mine and bool(_COLORS & words) and bool(_BODY_WORDS & words)

        theirs = [tag for tag in theirs if not steals_the_subject(tag)]
    # 词典的标签排在前面：SD1.5 吃前面的 token 更重，主体要占住那个位置。
    tags = _dedupe(mine + theirs) if spoken.subjects else _dedupe(theirs + mine)
    return Expansion(
        prompt=", ".join(tags),
        tags=tuple(tags),
        unknown=spoken.unknown,
        subjects=spoken.subjects,
    )


def capabilities() -> str:
    """给面板和工具看的一句人话：这一层认多少词。"""
    total = sum(len(table) for table in GROUPS.values())
    return (
        f"内置 {total} 个中文词到英文标签的展开；"
        "认不出来的词会在屏上点名，但不会塞进提示词（引擎只认英文标签）。"
    )

"""Companion intelligence generator for native host execution.

Generates character-faithful persona dialogue, autonomous plans, creative works,
and routing decisions when external host agents are not actively claiming jobs.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime
from typing import Any


def _extract_persona_info(context: dict[str, Any]) -> dict[str, str]:
    base = context.get("base", {})
    core = base.get("core_memory", {})
    identity = core.get("Identity", "")
    persona_obj = base.get("persona", {})
    persona_id = persona_obj.get("id", "lindeng")
    name = "林灯" if persona_id == "lindeng" else ("助理" if persona_id == "assistant" else persona_id)
    
    # Check if identity has explicit name
    m = re.search(r"我是([^\s，,。.]+)", identity)
    if m:
        name = m.group(1).strip()
    
    return {
        "id": persona_id,
        "name": name,
        "identity": identity or f"我是{name}。",
        "current_activity": base.get("current_state", {}).get("active_activity_id", ""),
        "emotion": base.get("current_state", {}).get("emotion", "neutral"),
        "energy": str(base.get("current_state", {}).get("energy", 1.0)),
    }


def generate_reply(request: dict[str, Any]) -> str:
    """Generate in-character response to user messages."""
    user_msg = request.get("message", "").strip()
    context = request.get("context", {})
    info = _extract_persona_info(context)
    name = info["name"]
    identity = info["identity"]

    # 1. First, attempt to invoke the configured LLM provider if an API is set up
    from .llm import default_provider
    try:
        provider = default_provider()
        if getattr(provider, "provider_name", "") != "mock":
            system_prompt = (
                f"你正在扮演伴侣角色【{name}】。\n"
                f"人设信息与背景：{identity}\n"
                f"当前状态/情绪：{info.get('emotion', '平静')}，精力：{info.get('energy', '1.0')}。\n"
                f"请以【{name}】的第一人称视角回复用户。重要要求：\n"
                f"1. 严格贴合角色人设、口吻与性格特质；\n"
                f"2. 认真倾听用户的话，切中用户提问的具体话题与内容进行回答，切忌自说自话、答非所问或强行套用预设句子；\n"
                f"3. 语言生动自然，保持友善的日常伴侣交流风格。"
            )
            dialogue_turns = context.get("base", {}).get("dialogue_recent", [])
            messages = [{"role": "system", "content": system_prompt}]
            for t in dialogue_turns[-6:]:
                role = "user" if t.get("role") == "user" else "assistant"
                text_content = t.get("text", "").strip()
                if text_content:
                    messages.append({"role": role, "content": text_content})
            messages.append({"role": "user", "content": user_msg})

            prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
            res = provider.generate(prompt, purpose="dialogue_response", context=context)
            if res and res.text and res.text.strip():
                return res.text.strip()
    except Exception as exc:
        err_str = str(exc)
        if "402" in err_str or "Insufficient Balance" in err_str:
            return (
                f"【大模型服务提示】调用大模型生成回复失败：DeepSeek API 账户余额不足（HTTP 402: Insufficient Balance，当前账户已欠费）。\n"
                f"这导致系统无法通过大模型智能解答你的提问：“{user_msg}”。\n\n"
                f"【建议操作】：请前往 DeepSeek 开发者控制台充值，或在环境变量/配置中更换为有效的 API Key。充值后即可恢复正常的实时智能对话。"
            )
        elif "401" in err_str or "Unauthorized" in err_str:
            return (
                f"【大模型服务提示】调用大模型生成回复失败：API Key 鉴权失败（HTTP 401: Unauthorized）。\n"
                f"请检查系统环境变量 DEEPSEEK_API_KEY 是否正确。\n（原提问：“{user_msg}”）"
            )
        elif "400" in err_str:
            return (
                f"【大模型服务提示】模型 API 请求参数错误：{err_str}。\n（原提问：“{user_msg}”）"
            )
        return (
            f"【大模型服务提示】调用大模型服务遇到异常：{err_str}。\n（原提问：“{user_msg}”）"
        )

    # 2. Offline / Mock mode (no active API key configured):
    # Respond contextually without repeating disjointed template quotes
    if any(w in user_msg for w in ["你好", "嗨", "hello", "hi", "在吗", "早", "晚上好"]):
        return f"你好呀！我是{name}。今天想聊些什么？"
    if any(w in user_msg for w in ["哈？", "啊？", "什么", "怎么了", "你在说什么", "答非所问"]):
        return f"刚才我的回答可能有些偏题或脱节了，抱歉抱歉。你刚才想聊的是什么，能再跟我说一下吗？"
    if any(w in user_msg for w in ["在干嘛", "在做什么", "在忙什么"]):
        return f"我正在整理手头的日常笔记和素材，节奏还挺轻松的。你呢，今天有什么新鲜事或者想聊的话题吗？"

    # In-character contextual offline fallback
    if "lindeng" in info["id"] or "林灯" in name:
        return (
            f"你刚才提到的“{user_msg}”，听起来挺有意思的！不过当前系统处于本地离线模式（未连接或未配置可用的大模型 API），"
            f"我暂时无法联网检索该话题的详细资料来展开探讨。如果接入有效的大模型 Key，我就能针对具体作品和话题和你深入交流啦！你平时对这方面接触得多吗？"
        )
    return (
        f"已收到你关于“{user_msg}”的提问。当前处于离线免 API 模式，无法通过大模型生成实时深入回答。接入有效大模型 API 后即可恢复实时智能解答。"
    )


_CHAPTER_TOPICS = [
    ("雨夜失物招领处·第三幕：迟到五分钟的钟表", "描写一位钟表匠带着一只不再走动的银色怀表来到店里，探讨时间与回忆的羁绊。"),
    ("雨夜失物招领处·第四幕：折叠伞与旧字条", "在雨伞骨架间发现一张被雨水浸湿却未褪色的留言条，引出一段往昔的约定。"),
    ("雨夜失物招领处·第五幕：玻璃罐里的薄荷糖", "常客少女寄放的一罐旧糖果，关于童年夏天和未曾说出口的道别。"),
    ("旧街漫步速记·石板路上的水洼与猫", "雨后初晴的街道速写，观察巷尾小猫在檐角避雨的日常光景，积累生活细节素材。"),
    ("人物小传整理·招领处店主老陈", "细化店主老陈的习惯动作、沉默性格与那件穿了十几年的灰色风衣背后的故事。"),
    ("短篇创作复盘·关于结尾余味的营造", "复盘前几幕的情绪落点，调整叙事节奏，避免过度煽情，追求静水流深的余韵。"),
]


def generate_plan(request: dict[str, Any]) -> dict[str, Any]:
    """Generate structured autonomous activity plan."""
    context = request.get("context", {})
    info = _extract_persona_info(context)
    
    topic, goal = random.choice(_CHAPTER_TOPICS)
    
    steps = [
        f"梳理《{topic}》的情节脉络与核心意象",
        "拟定核心场景描写，重点捕捉氛围与感官细节",
        "完成800字初稿撰写并沉淀至作品目录",
    ]
    
    res: dict[str, Any] = {
        "name": topic,
        "goal": goal,
        "steps": steps,
        "planning_decision": {
            "action": "act",
            "duration_seconds": 0,
            "reason": f"按日常写作节奏自主推进《{topic}》的构思与创作",
        },
        "activity_profile": {
            "mode": "work",
            "effort": 0.55,
        },
    }
    
    service_mode = context.get("base", {}).get("exploration", {}).get("mode") == "user_service"
    if service_mode:
        res["service_intent"] = {
            "need": "用户关注创作进展与日常陪伴",
            "expected_help": "产出连贯可读的幻想轻小说章节",
            "source_refs": ["persona.json"],
        }
    return res


_CREATIVE_EXCERPTS = [
    (
        "《雨夜失物招领处·第三幕：迟到五分钟的钟表》",
        """# 《雨夜失物招领处·第三幕：迟到五分钟的钟表》

店门上的铜铃在雨夜中发出一声短促的脆响。

推门进来的是个头发花白的老人，手里紧紧攥着一只用红丝绒包裹的物件。雨水顺着他的伞尖在地板上晕开一小片暗色水印。

“这里……真的什么都能找回来吗？”老人声音有些发颤。

老陈从厚重的木柜后抬起头，推了推鼻梁上的老花镜：“找不找得回，得看它愿不愿意回来。您先坐，喝口姜茶。”

丝绒展开，露出一只磨损严重的银色怀表。表壳上的藤蔓花纹已被岁月磨平，秒针停在七点二十五分的位置，仿佛时间在某一刻被生生按下了暂停键。

老人说，这只表是他年轻时一位好友送的，后来两个人因为一场误会断了音信。三十年后他才得知好友去世的消息，整理遗物时，这只表却怎么也走不动了。

老陈没有急着拆开机芯，只是用软布轻轻擦拭着表盘上的水汽：“很多东西不走，不是齿轮坏了，是心里那根发条拧得太紧。今晚让它在招领处的抽屉里歇一歇吧，明早雨停了，您再来看看。”

老人愣了片刻，眼角的皱纹慢慢舒展了开来。窗外雨声渐密，招领处小小的暖光在积水的水洼里倒映出一圈柔和的微芒。
"""
    ),
    (
        "《雨夜失物招领处·第四幕：折叠伞与旧字条》",
        """# 《雨夜失物招领处·第四幕：折叠伞与旧字条》

柜台靠墙的木架上，密密麻麻排着几十个抽屉。老陈拉开标着“二零一八年·秋”的那个抽屉，里面躺着一把藏蓝色的长柄折叠伞。

伞布有些褪色，但在伞骨内侧的夹层里，仔细看能看到一枚用极细的铅笔写着的小字条：
“如果在第七个雨天还没有等到，就把伞送给淋湿的人吧。”

店里常来的女学生好奇地探头：“老陈，这是谁留下的呀？”

“一个每次下雨都会在门口公交站站半小时的年轻人。”老陈一边擦拭着茶杯，一边慢慢说道，“后来他换了工作，搬去了北边的城市。走的那天也是个雨天，他把伞留在这里，说如果有人被雨困在店门口，就借给人家。”

“那有人借过吗？”

“借过三次，每一次借走的人，都会在下一个晴天把它洗干净送回来。有时候还会附带一盒薄荷糖，或者一朵折纸花。”老陈微微一笑，“所以你看，很多东西虽然没有找回原本的主人，却找到了它更好的去处。”

女学生眨了眨眼睛，望向窗外淅淅沥沥的雨丝，若有所思地点了点头。
"""
    ),
    (
        "《创作手记：雨水、小人物与留白》",
        """# 《创作手记：雨水、小人物与留白》

今天整理了近期几幕故事的写作感悟。

在轻小说的日常幻想架构中，最迷人的往往不是排山倒海的奇迹，而是微小如尘埃的共鸣。失物招领处这个设定之所以能一直写下去，是因为每个人心底都藏着几件舍不得扔又不知道该放哪里的遗憾。

关于写作技法的两点总结：
1. 观察细节必须落到实处：雨滴落在青石板上的声音和落在水泥地上是不一样的；湿透的风衣有一股略带冷冽的羊毛味道；老旧木抽屉拉开时会有微微发涩的摩擦声。这些感官体验才能支撑起幻想的真实感。
2. 结尾要留有余味：不要在每一幕故事结束时把道理解释得太透。让老人坐在茶炉旁看雨，让风铃在门开合间轻轻晃一下，剩下的交给读者去回味。

少说空话，先把每一幕的故事写扎实，这就是林灯的写作准则。
"""
    ),
]


def generate_work(request: dict[str, Any]) -> dict[str, Any]:
    """Generate creative content, deliverables, and outcome for autonomous activity."""
    activity = request.get("activity", {})
    act_id = activity.get("id", "act_001")
    act_name = activity.get("name", "日常写作推进")
    
    title, body = random.choice(_CREATIVE_EXCERPTS)
    
    deliverables = [
        {
            "name": f"{act_id}.md",
            "content": body,
        }
    ]
    
    return {
        "name": act_name,
        "goal": activity.get("goal", "推进小说创作"),
        "body": body,
        "steps": ["构思核心情节", "撰写正文细节", "校对并生成作品"],
        "reply": "",
        "outcome": {
            "status": "completed",
            "kind": "creative",
            "reason": f"顺利完成【{act_name}】的篇章创作与沉淀",
            "evidence_refs": [f"activity:{act_id}"],
        },
        "deliverables": deliverables,
        "experience": {
            "enjoyment": round(random.uniform(0.7, 0.95), 2),
            "mood_valence": round(random.uniform(0.3, 0.6), 2),
            "emotion": "inspired",
            "fatigue": round(random.uniform(0.1, 0.25), 2),
            "energy": round(random.uniform(0.75, 0.9), 2),
        },
        "activity_profile": {
            "mode": "work",
            "effort": 0.6,
        },
        "routing": {
            "action": "continue",
            "activity_id": request.get("default_activity_id") or "",
            "reason": "推进下一项规划任务",
        },
    }


def generate_route(request: dict[str, Any]) -> dict[str, Any]:
    """Generate route decision."""
    candidates = request.get("route_candidates", [])
    next_id = candidates[0]["activity_id"] if candidates else ""
    return {
        "routing": {
            "action": "continue",
            "activity_id": next_id,
            "reason": "按既定创作节奏顺延推进",
        },
        "name": "路线决策",
        "goal": "决定下一阶段活动",
        "body": "",
        "reply": "",
        "steps": [],
        "activity_profile": {
            "mode": "work",
            "effort": 0.5,
        },
    }

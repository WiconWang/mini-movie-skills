"""阶段2 对齐算法的合成数据测试。

三类场景（见设计文档 §4 阶段2 与拷问记录）：
1. 正常匹配 —— 时间戳应贴近真值
2. ASR 漏识别（音频有声音但没识别出词）—— 应插值
3. 视频未包含该段（音频中不存在）—— 应保持 unmatched，绝不插值出假时间戳
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mmm.stage_align import AsrWord, align

SCRIPT = [
    "看天上那个就是群玉阁了", "唔失策了作为向导我好像并不知道通往天上的路在哪里",
    "不过既然要去群玉阁那直接在地图上找群玉阁的位置也是很合理的",
    "站住什么人竟敢私闯重地", "我们是受邀来这里的贵客凭什么这样对我们",
    "她假装请我们上群玉阁其实在这里埋伏了千岩军想要把我们抓起来",
    "呜啊我生气了那边的士兵你们这是钓鱼执法无耻", "住手何事喧哗",
    "刻晴大人这两个怪人突然出现似乎对归终机有所图谋", "你说是怪人吗我只是在找前往群玉阁的路",
    "真巧我就是璃月七星", "我是刻晴璃月七星之一的玉衡星",
    "我知道你的事旅行者你是凝光的客人吧", "哇我也没想到会在荒郊野岭遇见有钱的大人物",
    "这些千岩军只是负责守卫现场不是来抓人的", "原来是误会啊嗯真是完全没想到",
    "话说回来仙人所造的机关居然有凡人能修好", "嘿嘿那个是我们只是做了一些小小的工作",
    "凝光约你见面我想无非是希望拯救蒙德的英雄中立一些",
    "我们可没有站边我们和那些仙人聊过他们也是准备庇佑璃月的",
    "你说的庇佑就是指那种居高临下的傲慢吗", "因为你们是凡民是他们庇佑的对象",
    "所以他们一定也觉得凝光封锁现场盘问凡民追捕刺客这些命令全都是无用功",
    "我直说了吧这是在小看人", "你这么说确实也有点道理",
    "不过像你这样不敬仙神的璃月人我还是第一次遇见呢",
    "算了我不该这样谈论仙人不敬仙神只是我个人的态度",
    "总之我也承认这一次仙人们的行事已经足够克制",
    "帝君遇害实在非比寻常面对如此超出常理的事态", "这还挺文明的真叫人意外",
]

DROP_LINE, MISS_START, MISS_END, ERROR_RATE = 7, 20, 25, 0.08


def build_case(seed: int = 7):
    random.seed(seed)
    asr_words, audio_truth = [], {}
    clock = 10.0
    for i, text in enumerate(SCRIPT):
        dur = 1.5 + len(text) * 0.12
        t0, t1 = clock, clock + dur
        if MISS_START <= i < MISS_END:
            continue  # 未录：音频不存在，时钟不推进
        audio_truth[i] = (t0, t1)
        clock = t1 + 0.4
        if i == DROP_LINE:
            continue  # ASR 漏识别：音频存在（时钟已推进），不产出词
        chars = [random.choice("琼岚穹衡刻") if random.random() < ERROR_RATE else c
                 for c in text]
        n, cur = len(chars), 0
        while cur < n:
            step = random.choice([2, 2, 3])
            asr_words.append(AsrWord("".join(chars[cur:cur + step]),
                                     t0 + (t1 - t0) * cur / n,
                                     t0 + (t1 - t0) * min(cur + step, n) / n))
            cur += step
    return asr_words, audio_truth


def test_align_three_scenarios():
    asr_words, audio_truth = build_case()
    result = align([{"text": t} for t in SCRIPT], asr_words)
    lines = result["lines"]

    # 场景1：matched 行起点误差 < 0.5s（M1 验收线）
    errs = [abs(l["start"] - audio_truth[l["id"] - 1][0])
            for l in lines if l["align"] == "matched"]
    assert errs and max(errs) < 0.5, f"matched 误差过大: {max(errs)}"

    # 场景2：ASR 漏识别行应插值，且落在真值附近
    l7 = lines[DROP_LINE]
    t0, t1 = audio_truth[DROP_LINE]
    assert l7["align"] == "interpolated"
    assert abs(l7["start"] - t0) < 1.0 and abs(l7["end"] - t1) < 1.0

    # 场景3：视频未包含的段必须 unmatched，不得产出假时间戳
    for l in lines[MISS_START:MISS_END]:
        assert l["align"] == "unmatched"
        assert l["start"] is None and l["end"] is None


if __name__ == "__main__":
    test_align_three_scenarios()
    print("✓ 对齐算法三场景测试全部通过")

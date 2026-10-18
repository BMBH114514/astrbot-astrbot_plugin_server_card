# -*- coding: utf-8 -*-
# ============================================================
#  AI 点评  persona.py
#  换成你自己的人设就行，改 name 和 comment()
#  d 里能用的数据:
#     d["cpu"]          CPU 占用百分比 (float)
#     d["mem_percent"]  内存占用百分比
#     d["mem_total_gb"] 内存总量
#     d["proc_total"]   进程总数
#     d["up_h"] / d["up_days"]   已运行小时 / 天
#     d["free_c"] / d["free_d"]  剩余空间 GB
#     d["mood"] / d["mood_word"] 心情值与短语
#     d["top"]          内存占用排行 [(MB, 进程名), ...]
#     d["cfg"]          硬件信息，如 d["cfg"]["gpu"] / d["cfg"]["ram"]
# ============================================================

class Persona:

    name = "Full"

    def comment(self, d):
        bits = []

        # 开机时长
        if d["up_days"] >= 7:
            bits.append("连着开了 %d 天没关机，你是把它当机房在供" % d["up_days"])
        elif d["up_h"] >= 24:
            bits.append("开了 %d 小时，还记得重启俩字怎么写吗" % d["up_h"])

        # CPU
        if d["cpu"] <= 6:
            bits.append("CPU 才 %.0f%%，风扇估计都懒得转" % d["cpu"])
        elif d["cpu"] >= 70:
            bits.append("CPU 都干到 %.0f%% 了，别硬撑" % d["cpu"])

        # 内存
        if d["mem_percent"] >= 85:
            bits.append("内存 %d%%，眼看就要炸" % d["mem_percent"])
        elif d["mem_percent"] >= 70:
            bits.append("内存 %d%%，挤是挤了点但还能喘" % d["mem_percent"])

        # 显卡
        if "基本显示适配器" in d["cfg"].get("gpu", ""):
            bits.append("显卡还是基本显示适配器，能亮就不错了")

        # 磁盘
        if d["free_c"] < 20:
            bits.append("C 盘只剩 %.1f GB，该动手清了" % d["free_c"])

        # 内存大户
        if d["top"] and len(bits) < 3:
            nm = d["top"][0][1]
            nm = nm[:-4] if nm.lower().endswith(".exe") else nm
            bits.append("扛得最狠的是 %s，%.0f MB" % (nm, d["top"][0][0]))

        if not bits:
            bits.append("状态挺正常，没什么好挑的")

        s = "，".join(bits[:3]) + "。"
        if len(s) > 118:
            s = s[:117] + "…"
        return s

"""在线代码评测与竞赛系统 (Online Judge & Contest System).

Package layout:
  - config      全局配置与路径管理
  - storage     JSON 分片存储、原子写入与细粒度锁
  - sandbox     Docker / 原生子进程沙箱（资源限制、超时终止）
  - judge       评测引擎（队列 + 线程池 + 并发调度）
  - comparator  评测结果精确比对（浮点误差 / 多答案）
  - ranking     实时排行榜增量更新与封榜
  - cheat       防作弊检测
  - api         Flask 蓝图（认证、题目、竞赛、提交、榜单、论坛、统计、设置）
  - utils       通用工具与校验
"""

__version__ = "1.0.0"

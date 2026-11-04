"""渲染报告正文里的 Markdown。

报告本身用 Markdown 撰写 —— `reports/composition.py` 与 Agent 都在写它 —— 而它
过去是塞在 `<pre>` 里到达页面的，读者会原样看到 `## 重点项目` 和 `**加粗**`。
这是界面上唯一一处把文件格式当成报告展示的地方。

渲染方式由两条规则决定：

* **转义，不信任。** 报告会引用仓库 README，而那是贡献者能控制的第三方
  文本。`escape=True` 把源文本里的任何 HTML 都变成文字，于是 README 无法把
  标记偷带进界面。校验器已经会拒绝注入的指令；这里是第二道独立的防线，
  因为转义不依赖模型和守卫彼此达成一致。
* **不留原始 HTML 的逃生口。** 刻意不提供按报告单独开启的开关。一个写着
  「这份按 HTML 渲染」的字段，迟早会在没人重读的数据上被置为真。
"""

from __future__ import annotations

import mistune
from markupsafe import Markup

#: 表格与删除线是成稿报告实际用到的两个 GitHub 扩展；`url` 插件会把裸 URL 变成
#: 链接，证据章节里就含有这类 URL。
_RENDERER = mistune.create_markdown(
    escape=True,
    plugins=["table", "strikethrough", "url"],
)


def render_markdown(text: str | None) -> Markup:
    """把一份 Markdown 文档渲染成已净化的 HTML。

    空输入渲染成空输出而不是抛错：正文生成失败的报告是页面必须画出来的一种状态，
    不是一个 500。
    """
    if not text:
        return Markup("")
    return Markup(_RENDERER(text))

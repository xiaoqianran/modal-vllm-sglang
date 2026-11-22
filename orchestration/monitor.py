"""A file-based dashboard: no server, cloud credentials or private paths in media."""
from html import escape
import json
import shutil
import struct
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from orchestration import state
from runtime.utils.files import read_json, atomic_write


@lru_cache(maxsize=256)
def _dimensions(path, modified, size):
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(24)
        if header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR":
            return struct.unpack(">II", header[16:24])
        probe = shutil.which("ffprobe")
        if probe:
            result = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=width,height", "-of", "json", path],
                                    capture_output=True, text=True, timeout=5, check=True)
            info = json.loads(result.stdout)["streams"][0]
            return info["width"], info["height"]
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    return None


def dimensions(path):
    try:
        stat = path.stat()
        return _dimensions(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def duration(value):
    if value is None:
        return "未采集"
    if value < 1000:
        return f"{value:.0f} 毫秒"
    if value < 60000:
        return f"{value / 1000:.1f} 秒"
    return f"{int(value // 60000)} 分 {int(value // 1000) % 60} 秒"


def stage_table(rows):
    body = "".join(f"<tr><td>{escape(row.get('label', row['stage']))}</td><td>{duration(row.get('duration_ms'))}</td>"
                   f"<td>{'未采集' if row.get('status') == 'unknown' else '进行中' if row.get('status') == 'running' else '已记录'}</td>"
                   f"<td>{escape(row.get('source', '实测'))}</td></tr>" for row in rows)
    return '<div class="table-wrap"><table><thead><tr><th>阶段</th><th>耗时</th><th>状态</th><th>来源</th></tr></thead><tbody data-field="stages">' + body + '</tbody></table></div>'


def job_markup(job=None):
    job = job or {}
    asset = job.get("asset")
    preview = "等待成品"
    filename, href, specs = "", "#", "实际尺寸：未采集"
    if asset:
        href, filename = escape(asset["url"], quote=True), escape(asset["filename"])
        preview = (f'<video controls preload="metadata" src="{href}"></video>' if asset["kind"] == "video" else
                   f'<a href="{href}" target="_blank"><img loading="lazy" src="{href}" alt="{filename}"></a>')
        dims = asset.get("dimensions")
        if dims:
            specs = f"实际：{dims[0]} × {dims[1]}"
    identifier = escape("job-" + job.get("key", ""), quote=True)
    reason = escape(job.get("reason") or "")
    return (f'<article class="job" id="{identifier}"><header class="job-head"><h3 data-field="step">{escape(job.get("step", ""))}</h3>'
            f'<span class="badge {escape(job.get("status", ""))}" data-field="status">{escape(job.get("status_label", ""))}</span></header>'
            f'<div class="job-content"><div class="preview" data-field="media">{preview}</div>'
            f'<a class="filename" href="{href}" data-field="file">{filename}</a><div class="specs" data-field="specs">{specs}</div>'
            f'<div class="reason" data-field="reason">{reason}</div>'
            '<div class="quick-times"><span>生成 <b data-field="generate">未采集</b></span><span>已观测 <b data-field="elapsed">未采集</b></span></div>'
            '<details><summary>阶段耗时与下载</summary>' + stage_table(job.get("stages", [])) +
            '<p class="note">阶段可能并行，不能相加当作镜头总耗时。生成阶段为服务调用观测，可能包含引擎内部排队。</p><p class="specs" data-field="download"></p><p class="note" data-field="execution"></p></details>'
            '<details><summary>提示词、参数与调用记录</summary><pre data-field="parameters"></pre></details></div></article>')


def model_markup():
    return ('<article class="model"><header class="job-head"><h3 data-field="name"></h3><span class="badge" data-field="status"></span></header>'
            '<div class="reason" data-field="reason"></div><p class="note" data-field="summary"></p><p class="specs" data-field="download"></p>'
            + stage_table([]) + '<a class="small-link" data-field="log" hidden>查看准备日志</a></article>')


def safe_json(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render(output):
    from orchestration.monitor_data import build_snapshot
    output = Path(output).resolve()
    runtime = state.runtime_root(output)
    css = Path(__file__).with_name("monitor.css").read_text(encoding="utf-8")
    script = Path(__file__).with_name("monitor.js").read_text(encoding="utf-8")
    for path in sorted((runtime / "tasks").glob("*/*/plan.json")):
        directory = path.parent
        if not (directory / "state.json").exists() or state.layout(directory).get("output") != str(output):
            continue
        plan, current = read_json(path), read_json(directory / "state.json")
        data = build_snapshot(directory, plan, current, dimensions)
        encoded = safe_json(data)
        atomic_write(runtime / "monitor" / (plan["name"] + ".js"), ("window.updateTaskMonitor(" + encoded + ");").encode("utf-8"))
        by_key = {job["key"]: job for job in data["jobs"]}
        cards = []
        for shot in data["shots"]:
            jobs = "".join(job_markup(by_key[key]) for key in shot["keys"])
            cards.append(f'<section class="shot" id="shot-{escape(shot["item"], quote=True)}"><header class="shot-head"><h2>镜头 {escape(shot["item"])}</h2>'
                         f'<span class="muted" data-shot-summary></span></header><div class="shot-jobs">{jobs}</div></section>')
        title = escape(plan["name"])
        feed = escape(quote("../.runtime/monitor/" + plan["name"] + ".js"), quote=True)
        document = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>监控{title}</title><style>{css}</style></head><body data-feed="{feed}"><main>'
                    f'<header><span class="eyebrow">GENERATION MONITOR</span><h1>{title}</h1><span id="task-status" class="badge">{escape(data["status_label"])}</span>'
                    '<div class="toolbar"><button id="refresh">刷新进度</button><label><input type="checkbox" id="auto-refresh" checked> 自动更新（5秒）</label><span id="connection">连接本地运行记录</span></div></header>'
                    '<div class="summary-grid"><div class="metric">生成完成<b id="progress">—</b></div><div class="metric">已下载到本地<b id="downloaded">—</b></div>'
                    '<div class="metric">失败步骤<b id="failed">—</b></div><div class="metric">任务已观测耗时<b id="elapsed">—</b><small id="timing-source" class="muted"></small></div></div>'
                    '<section class="section"><p id="request-summary"></p><div class="task-meta"><span>开始：<b id="started"></b></span><span>完成：<b id="finished"></b></span><span>数据更新：<b id="updated"></b></span></div>'
                    f'<div id="refresh-progress"><i></i></div><p class="note">图片和视频展示实际分辨率。平台排队、快照恢复没有证据时显示未采集。</p><a class="small-link" href="{escape(data["log_url"], quote=True)}">查看任务事件日志</a></section>'
                    '<section id="task-stages" class="section"><details><summary>任务阶段累计耗时</summary>' + stage_table(data["stages"]) +
                    '<p class="note">累计耗时可能并行或包含失败尝试，不能相加作为总耗时。缓存复用的文件校验不算重新下载。</p></details></section>'
                    '<div id="shots">' + ''.join(cards) + '</div>'
                    '<section class="section"><details><summary>共用模型准备与启动</summary><p class="note">这里展示共用服务的最新准备记录，不重复计入每个镜头。构建与部署目前记录组合耗时；权重字节速度未采集时不显示推测值。</p><div id="models" class="model-grid"></div>'
                    '<div id="shared-stages"><h3>共用容器启动（实测片段）</h3>' + stage_table([s for s in data["shared_startup"] if s["stage"] == "startup"]) +
                    '<p class="note">启动片段可能来自多个容器，不是每个镜头都发生一次；不把快照历史模型加载时间作为本次耗时。</p></div></details></section>'
                    '<p class="note">默认图片档位：1K（长边1024）。历史任务保留原始尺寸；不支持的原生视频档位需要显式选择。</p></main>'
                    '<template id="job-template">' + job_markup() + '</template><template id="model-template">' + model_markup() + '</template>'
                    '<script type="application/json" id="initial-data">' + encoded + '</script><script>' + script + '</script></body></html>')
        atomic_write(output / ("监控" + plan["name"] + ".html"), document.encode("utf-8"))


class LogStream:
    """Preserve terminal output while bounding the local worker log."""
    def __init__(self, original, path):
        import logging
        from logging.handlers import RotatingFileHandler
        self.original = original
        self.handler = RotatingFileHandler(path, maxBytes=8 * 1024 * 1024, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

    def write(self, value):
        import logging
        self.original.write(value)
        if value.strip():
            self.handler.handle(logging.LogRecord("worker", logging.INFO, "", 0, value.rstrip(), (), None))
        return len(value)

    def flush(self):
        self.original.flush()
        self.handler.flush()

    def close(self):
        self.handler.close()

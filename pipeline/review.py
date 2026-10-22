# -*- coding: utf-8 -*-
"""人工校对：生成单文件 HTML（无依赖、可离线、进度存 localStorage），导出 patch 再合回。"""
import json
from typing import Any, Dict, List

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif;background:#f5f6f8;color:#1f2937}
header{position:sticky;top:0;z-index:9;background:#fff;border-bottom:1px solid #e5e7eb;padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:700}
.bar{flex:1;height:6px;background:#eef1f5;border-radius:3px;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;background:#10b981;width:0}
button{font:inherit;padding:5px 12px;border:1px solid #d1d5db;background:#fff;border-radius:6px;cursor:pointer}
button:hover{background:#f3f4f6}
button.p{background:#111827;color:#fff;border-color:#111827}
main{padding:16px;max-width:1180px;margin:0 auto}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:14px;overflow:hidden}
.card.ok{border-left:4px solid #10b981}
.card.bad{border-left:4px solid #ef4444;background:#fffbfb}
.hd{display:flex;gap:10px;align-items:center;padding:8px 14px;background:#fafbfc;border-bottom:1px solid #eef0f3;font-size:13px}
.no{font-weight:700}
.tag{color:#6b7280}
.st{margin-left:auto;font-size:12px;color:#9ca3af}
.bd{display:grid;grid-template-columns:1fr 1fr;gap:0}
.col{padding:12px 14px}
.col.raw{border-right:1px solid #eef0f3;background:#fcfcfd}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12.5px/1.7 "Consolas","Microsoft YaHei",monospace;color:#374151;max-height:340px;overflow:auto}
label{display:block;font-size:12px;color:#6b7280;margin:8px 0 2px}
input,textarea,select{width:100%;font:inherit;padding:5px 8px;border:1px solid #d1d5db;border-radius:6px;background:#fff}
textarea{min-height:74px;resize:vertical}
textarea.sm{min-height:46px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.ops{padding:0 14px 12px;display:flex;gap:8px}
@media(max-width:820px){.bd{grid-template-columns:1fr}.col.raw{border-right:0;border-bottom:1px solid #eef0f3}}
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <div class="bar"><i id="pg"></i></div>
  <span id="cnt" class="tag"></span>
  <button id="flt">只看未核对</button>
  <button id="nxt">下一个未核对</button>
  <button class="p" id="exp">导出 patch</button>
</header>
<main id="main"></main>
<script>
var DATA = __DATA__;
var KEY = "408-review-" + "__TITLE__";
var state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { state = {}; }
var onlyUnchecked = false;

function el(t, c, x){ var e = document.createElement(t); if(c) e.className = c; if(x !== undefined) e.textContent = x; return e; }
function get(id){ if(!state[id]) state[id] = {status:"", fields:{}}; return state[id]; }
function save(){ try { localStorage.setItem(KEY, JSON.stringify(state)); } catch(e){} }

function field(id, key, label, val, kind){
  var wrap = el("div");
  wrap.appendChild(el("label", null, label));
  var inp;
  if (kind === "ta") { inp = el("textarea"); inp.value = val || ""; }
  else if (kind === "sm") { inp = el("textarea", "sm"); inp.value = val || ""; }
  else if (kind === "sel") {
    inp = el("select");
    ["", "ds", "co", "os", "cn"].forEach(function(v){
      var o = el("option", null, v || "-"); o.value = v; inp.appendChild(o);
    });
    inp.value = val || "";
  } else {
    inp = el("input"); inp.value = (val === null || val === undefined) ? "" : val;
    if (kind === "num") inp.type = "number";
  }
  if (inp.tagName === "TEXTAREA") {
    inp.addEventListener("input", function(){ inp.style.height = "auto"; inp.style.height = (inp.scrollHeight + 12) + "px"; });
  }
  inp.addEventListener("input", function(){
    var s = get(id); s.fields[key] = (kind === "num") ? (inp.value === "" ? null : parseInt(inp.value, 10)) : inp.value;
    save();
  });
  wrap.appendChild(inp);
  return wrap;
}

function render(){
  var main = document.getElementById("main");
  main.innerHTML = "";
  var total = DATA.length, done = 0;
  DATA.forEach(function(q){
    var s = get(q.id);
    if (s.status) done++;
    if (onlyUnchecked && s.status) return;
    var card = el("div", "card " + (s.status === "ok" ? "ok" : (s.status === "bad" ? "bad" : "")));
    card.id = "c-" + q.id;
    var hd = el("div", "hd");
    hd.appendChild(el("span", "no", "#" + String(q.seq).padStart(2, "0")));
    hd.appendChild(el("span", "tag", (q.type === "single" ? "单选" : "综合") + " · " + (q.subject || "?")));
    var st = el("span", "st", s.status === "ok" ? "已核对通过" : (s.status === "bad" ? "已标记问题" : "待核对"));
    hd.appendChild(st);
    card.appendChild(hd);

    var bd = el("div", "bd");
    var raw = el("div", "col raw");
    raw.appendChild(el("label", null, "原卷文本（只读，用来对拍）"));
    var pre = el("pre"); pre.textContent = q.raw || ""; raw.appendChild(pre);
    bd.appendChild(raw);

    var f = el("div", "col");
    var r1 = el("div", "row");
    r1.appendChild(field(q.id, "subject", "科目", q.subject, "sel"));
    r1.appendChild(field(q.id, "score", "分值", q.score, "num"));
    f.appendChild(r1);
    f.appendChild(field(q.id, "stem", "题干", q.stem, "ta"));
    if (q.type === "single") {
      var ro = el("div", "row");
      ["A","B","C","D"].forEach(function(k){
        var ff = field(q.id, "opt" + k, "选项 " + k, (q.options || {})[k], "sm");
        ro.appendChild(ff);
      });
      f.appendChild(ro);
      f.appendChild(field(q.id, "answer", "答案", q.answer, ""));
    }
    f.appendChild(field(q.id, "analysis", "解析", q.analysis, "ta"));
    f.appendChild(field(q.id, "kps", "知识点 id（逗号分隔）", (q.kp_ids || []).join(","), ""));
    bd.appendChild(f);
    card.appendChild(bd);

    var ops = el("div", "ops");
    var bok = el("button", null, "通过 (1)");
    bok.onclick = function(){ get(q.id).status = "ok"; save(); render(); };
    var bbad = el("button", null, "标记问题 (2)");
    bbad.onclick = function(){ get(q.id).status = "bad"; save(); render(); };
    ops.appendChild(bok); ops.appendChild(bbad);
    card.appendChild(ops);
    main.appendChild(card);
  });
  document.getElementById("pg").style.width = (total ? done * 100 / total : 0) + "%";
  document.getElementById("cnt").textContent = done + " / " + total + " 已核对";
}

document.getElementById("flt").onclick = function(){ onlyUnchecked = !onlyUnchecked; this.textContent = onlyUnchecked ? "显示全部" : "只看未核对"; render(); };
document.getElementById("nxt").onclick = function(){
  for (var i = 0; i < DATA.length; i++) {
    if (!get(DATA[i].id).status) { var e = document.getElementById("c-" + DATA[i].id); if (e) e.scrollIntoView({behavior:"smooth", block:"center"}); return; }
  }
};
document.getElementById("exp").onclick = function(){
  var out = DATA.map(function(q){
    var s = get(q.id);
    if (!s.status && Object.keys(s.fields).length === 0) return null;
    var rec = { id: q.id, status: s.status || "pending", fields: s.fields };
    return rec;
  }).filter(Boolean);
  var blob = new Blob([JSON.stringify(out, null, 2)], { type: "application/json" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "review_patch.json";
  a.click();
};
render();
window.addEventListener("load", function(){
  document.querySelectorAll("textarea").forEach(function(t){ t.style.height = "auto"; t.style.height = (t.scrollHeight + 12) + "px"; });
});
</script></body></html>"""


def generate_html(items: List[Dict[str, Any]], raws: Dict[int, Dict[str, str]],
                  out_path: str, title: str) -> str:
    payload = []
    for q in items:
        r = raws.get(q.get("seq"), {})
        row = dict(q)
        row["raw"] = (r.get("q") or "") + ("\n\n—— 答案/解析 ——\n" + r.get("a", "") if r.get("a") else "")
        payload.append(row)
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = HTML.replace("__TITLE__", title).replace("__DATA__", data)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def apply_patch(patch_path: str, draft_path: str, out_path: str) -> Dict[str, int]:
    """把 patch 合回 draft：字段级覆盖，status 落到 reviewed/flagged。"""
    with open(patch_path, encoding="utf-8") as f:
        patch = {p["id"]: p for p in json.load(f)}
    rows, n_ok, n_bad = [], 0, 0
    with open(draft_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            q = json.loads(line)
            p = patch.get(q["id"])
            if p:
                fs_ = p.get("fields") or {}
                for k, v in fs_.items():
                    if k.startswith("opt"):
                        q.setdefault("options", {})[k[3:]] = v
                    elif k == "kps":
                        q["kp_ids"] = [x.strip() for x in (v or "").split(",") if x.strip()]
                    else:
                        q[k] = v
                q["status"] = "reviewed" if p.get("status") == "ok" else (
                    "flagged" if p.get("status") == "bad" else "draft")
                n_ok += q["status"] == "reviewed"
                n_bad += q["status"] == "flagged"
            rows.append(q)
    with open(out_path, "w", encoding="utf-8") as f:
        for q in rows:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    return {"total": len(rows), "reviewed": n_ok, "flagged": n_bad}

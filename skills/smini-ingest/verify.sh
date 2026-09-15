#!/usr/bin/env bash
# smini-ingest 交付验证
#
# 覆盖：
#   正向  多形态摄入 / 契约校验通过 / 内容保真 / 幂等（同内容两次 ID 一致）
#   反向  非法 checksum / 非 base64 内容 / 多余字段 / 缺必填字段 / Web 源引导
#
# 用法：bash skills/smini-ingest/verify.sh
# 退出码：0 = 全部通过；1 = 有失败

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PY="${SMINI_PY:-python3}"

cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT"

RUN="$ROOT/runs/verify-ingest"
rm -rf "$RUN"
mkdir -p "$RUN"

PASS=0
FAIL=0
FAILED_CASES=()

# 断言退出码：ok <期望码> <实际码> <用例名>
ok() {
    local want="$1" got="$2" name="$3"
    if [ "$want" = "$got" ]; then
        printf '  \033[32mPASS\033[0m  %s\n' "$name"
        PASS=$((PASS + 1))
    else
        printf '  \033[31mFAIL\033[0m  %s  (期望退出码 %s，实为 %s)\n' "$name" "$want" "$got"
        FAIL=$((FAIL + 1))
        FAILED_CASES+=("$name")
    fi
}

# 静默执行，只取退出码
quiet() { "$@" >/dev/null 2>&1; echo $?; }

SAMPLE='特斯拉公司成立于2003年，总部位于美国加利福尼亚州。马斯克是特斯拉的CEO。'
PYFILE="$ROOT/smini/steps/ingest.py"

echo
echo "════════ smini-ingest 验证 ════════"
echo

# ---------------------------------------------------------------- 正向用例
echo "── 正向：多形态摄入 ──"

# 1. text:// 显式源
code=$(quiet "$PY" -m smini.cli ingest --source "text://$SAMPLE" --out "$RUN/01-text.json")
ok 0 "$code" "1. text:// 源摄入成功"

# 2. 契约校验通过
code=$(quiet "$PY" -m smini.cli validate raw --in "$RUN/01-text.json")
ok 0 "$code" "2. text:// 产物通过 validate raw"

# 3. file:// 显式源（真实文件）
code=$(quiet "$PY" -m smini.cli ingest --source "file://$PYFILE" --out "$RUN/02-file.json")
ok 0 "$code" "3. file:// 源摄入真实文件成功"

code=$(quiet "$PY" -m smini.cli validate raw --in "$RUN/02-file.json")
ok 0 "$code" "4. file:// 产物通过 validate raw"

# 5. 隐式路径源（形如路径且存在 → 当文件）
code=$(quiet "$PY" -m smini.cli ingest --source "$PYFILE" --out "$RUN/03-implicit.json")
ok 0 "$code" "5. 隐式路径源被识别为文件"

# 6. 多源一次摄入（--source 可重复）
code=$(quiet "$PY" -m smini.cli ingest \
    --source "text://$SAMPLE" --source "file://$PYFILE" \
    --out "$RUN/04-multi.json")
ok 0 "$code" "6. 多源一次摄入成功"

n=$("$PY" -c "import json;print(len(json.load(open('$RUN/04-multi.json',encoding='utf-8'))))" 2>/dev/null)
if [ "$n" = "2" ]; then
    printf '  \033[32mPASS\033[0m  7. 多源产出 2 条 RawDocument\n'; PASS=$((PASS+1))
else
    printf '  \033[31mFAIL\033[0m  7. 多源应产出 2 条，实为 %s\n' "${n:-?}"; FAIL=$((FAIL+1)); FAILED_CASES+=("7. 多源产出条数")
fi

# 8. 内容保真：base64 还原后与原文逐字节一致（铁律 1：只搬字节，不解码）
"$PY" - "$RUN/01-text.json" "$SAMPLE" <<'EOF'
import base64, json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))[0]
got = base64.b64decode(d["content"]).decode("utf-8")
sys.exit(0 if got == sys.argv[2] else 1)
EOF
ok 0 "$?" "8. content 为 base64 且可还原为原文（未做任何解读）"

# 9. checksum 为 64 位十六进制 sha256
"$PY" - "$RUN/01-text.json" <<'EOF'
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))[0]
ok = bool(re.fullmatch(r"[0-9a-f]{64}", d["checksum"]))
ok = ok and d["checksum"] == d["source"]["checksum"]
sys.exit(0 if ok else 1)
EOF
ok 0 "$?" "9. checksum 为 sha256 且与 source.checksum 一致"

# 10. 幂等：同内容两次摄入，doc_id / checksum 完全一致
"$PY" -m smini.cli ingest --source "text://$SAMPLE" --out "$RUN/05-idem-a.json" >/dev/null 2>&1
"$PY" -m smini.cli ingest --source "text://$SAMPLE" --out "$RUN/05-idem-b.json" >/dev/null 2>&1
"$PY" - "$RUN/05-idem-a.json" "$RUN/05-idem-b.json" <<'EOF'
import json, sys
a = json.load(open(sys.argv[1], encoding="utf-8"))[0]
b = json.load(open(sys.argv[2], encoding="utf-8"))[0]
sys.exit(0 if (a["doc_id"], a["checksum"], a["source"]["source_id"])
              == (b["doc_id"], b["checksum"], b["source"]["source_id"]) else 1)
EOF
ok 0 "$?" "10. 幂等：同内容两次摄入 ID 一致"

# 11. ids raw 能从「工具路」产物（ID 全空）补算出相同 ID
"$PY" - "$RUN/05-idem-a.json" <<'EOF'
import json, subprocess, sys, tempfile, os
from pathlib import Path
src = json.load(open(sys.argv[1], encoding="utf-8"))[0]
# 模拟 A/B/C/D 路：工具给出文本，ID 全空
prop = dict(src, doc_id="")
prop["source"] = dict(src["source"], source_id="", checksum=None)
d = Path(tempfile.mkdtemp()) / "prop.json"
d.write_text(json.dumps([prop], ensure_ascii=False), encoding="utf-8")
r = subprocess.run([sys.executable, "-m", "smini.cli", "ids", "raw", "--in", str(d)],
                   capture_output=True, text=True,
                   env=dict(os.environ, PYTHONPATH=os.getcwd()))
if r.returncode != 0:
    sys.exit(1)
got = json.loads(d.read_text(encoding="utf-8"))[0]
sys.exit(0 if got["doc_id"] == src["doc_id"] and got["checksum"] == src["checksum"] else 1)
EOF
ok 0 "$?" "11. ids raw 为工具路产物补算出相同 ID"

# ---------------------------------------------------------------- 反向用例
echo
echo "── 反向：契约违规必须被拦下 ──"

# 12. 非法 checksum（非 sha256）→ validate 应失败
"$PY" - "$RUN/01-text.json" "$RUN/bad-checksum.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
d[0]["checksum"] = "not-hex"
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
EOF
code=$(quiet "$PY" -m smini.cli validate raw --in "$RUN/bad-checksum.json")
ok 1 "$code" "12. 非法 checksum 被 validate 拦下"

# 13. 缺必填字段 source → validate 应失败
"$PY" - "$RUN/01-text.json" "$RUN/bad-missing.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
d[0].pop("source")
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
EOF
code=$(quiet "$PY" -m smini.cli validate raw --in "$RUN/bad-missing.json")
ok 1 "$code" "13. 缺必填字段 source 被 validate 拦下"

# 14. 未声明的多余字段（additionalProperties: false）→ validate 应失败
"$PY" - "$RUN/01-text.json" "$RUN/bad-extra.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
d[0]["bogus_field"] = "x"
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
EOF
code=$(quiet "$PY" -m smini.cli validate raw --in "$RUN/bad-extra.json")
ok 1 "$code" "14. 未声明字段被 validate 拦下"

# 15. source_type 非法枚举值 → validate 应失败
"$PY" - "$RUN/01-text.json" "$RUN/bad-enum.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
d[0]["source"]["source_type"] = "carrier_pigeon"
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
EOF
code=$(quiet "$PY" -m smini.cli validate raw --in "$RUN/bad-enum.json")
ok 1 "$code" "15. 非法 source_type 枚举被 validate 拦下"

# 16. content 非 base64 → ids raw 应拒绝（不产出错误结果）
"$PY" - "$RUN/01-text.json" "$RUN/bad-b64.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
d[0]["content"] = "!!!not-base64!!!"
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
EOF
code=$(quiet "$PY" -m smini.cli ids raw --in "$RUN/bad-b64.json")
if [ "$code" != "0" ]; then
    printf '  \033[32mPASS\033[0m  16. 非 base64 content 被 ids raw 拒绝（退出码 %s）\n' "$code"
    PASS=$((PASS+1))
else
    printf '  \033[31mFAIL\033[0m  16. 非 base64 content 应被拒绝，却返回 0\n'
    FAIL=$((FAIL+1)); FAILED_CASES+=("16. 非 base64 content")
fi

# 17. Web 源走 E 路（Python 兜底）应报错，引导改用 A 路工具
code=$(quiet "$PY" -m smini.cli ingest --source "https://example.com/doc" --out "$RUN/bad-web.json")
if [ "$code" != "0" ]; then
    printf '  \033[32mPASS\033[0m  17. Web 源被 Python 兜底拒绝，引导走工具路由（退出码 %s）\n' "$code"
    PASS=$((PASS+1))
else
    printf '  \033[31mFAIL\033[0m  17. Web 源应被拒绝，却返回 0\n'
    FAIL=$((FAIL+1)); FAILED_CASES+=("17. Web 源路由")
fi

# ---------------------------------------------------------------- 汇总
echo
echo "════════════════════════════════════"
printf '  通过 \033[32m%s\033[0m 项，失败 \033[31m%s\033[0m 项\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo
    echo "  失败用例："
    for c in "${FAILED_CASES[@]}"; do echo "    - $c"; done
fi
echo "  产物目录：$RUN"
echo "════════════════════════════════════"
echo

[ "$FAIL" -eq 0 ] || exit 1

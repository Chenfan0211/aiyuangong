import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');
const dataDir = path.join(root, 'data', 'e2e');
const storeDb = path.join(dataDir, 'store_ops.db');
const metricsDb = path.join(dataDir, 'platform_metrics.db');
const chatDb = path.join(dataDir, 'agent_demo.db');
const evidenceDir = path.join(dataDir, 'evidence');
const fixturesPath = path.join(dataDir, 'fixtures.json');
const configPath = path.join(root, 'store_config.json');

const resolved = path.resolve(dataDir);
if (!resolved.startsWith(path.resolve(root) + path.sep)) {
  throw new Error(`Refusing to reset data outside repository: ${resolved}`);
}
fs.rmSync(dataDir, { recursive: true, force: true });
fs.mkdirSync(dataDir, { recursive: true });

const python = process.env.PYTHON || 'python';
const seedScript = `
import json
from datetime import datetime, timedelta
from pathlib import Path
from auth import AuthStore, totp_code
from platform_data import PlatformMetricStore
from task_store import TaskStore

root = Path(${JSON.stringify(root)})
data = Path(${JSON.stringify(dataDir)})
store_db = data / "store_ops.db"
metrics_db = data / "platform_metrics.db"
fixtures_path = data / "fixtures.json"
auth = AuthStore(store_db, tenant_id="LPL-001")
fixtures = {
    "sessionCookie": "store_ops_session",
    "csrfCookie": "store_ops_csrf",
    "csrfHeader": "X-CSRF-Token",
    "users": {}
}

def create_user(key, username, password, display_name, role):
    user = auth.create_user(username, password, display_name, role, "LPL-001", "WH-LION-001")
    item = {
        "user_id": user["user_id"],
        "username": username,
        "password": password,
        "display_name": display_name,
        "role": role,
    }
    if role in ("owner", "manager", "area_manager"):
        enrollment = auth.begin_mfa_enrollment(user["user_id"])
        auth.confirm_mfa_enrollment(user["user_id"], totp_code(enrollment["secret"]))
        item["totp_secret"] = enrollment["secret"]
    fixtures["users"][key] = item
    return item

employee = create_user("employee", "e2e_employee", "Employee-Secure-2026", "E2E 店员", "employee")
create_user("manager", "e2e_manager", "Manager-Secure-2026", "E2E 店长", "manager")
create_user("owner", "e2e_owner", "Owner-Secure-2026", "E2E 店主", "owner")

metrics = PlatformMetricStore(metrics_db, tenant_id="LPL-001")
header = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\\n")
for platform in ("meituan", "douyin"):
    lines = [header]
    base = 180 if platform == "meituan" else 140
    for offset in range(13, -1, -1):
        day = (datetime.now().date() - timedelta(days=offset)).isoformat()
        impressions = base + (13 - offset)
        if offset == 0:
            impressions = int(impressions * 0.62)
        views = max(1, int(impressions * 0.23))
        paid = max(1, int(views * (0.18 if offset else 0.08)))
        verified = max(1, paid - (3 if offset == 0 else 1))
        gmv = paid * 120
        refunds = 3 if offset == 0 else 0
        negatives = 2 if offset == 0 else 0
        rating = 3.8 if offset == 0 else 4.8
        lines.append(f"{day},{platform},WH-LION-001,{impressions},{views},{paid},{verified},{gmv},{refunds},2,{negatives},{rating}\\n")
    csv_path = data / f"seed-{platform}.csv"
    csv_path.write_text("".join(lines), encoding="utf-8")
    metrics.import_csv(csv_path, "WH-LION-001")
task_store = TaskStore(store_db, tenant_id="LPL-001")
today = datetime.now().date().isoformat()
due_at = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
tasks = []
for index in range(1, 9):
    task_id = f"e2e-task-{index:02d}"
    tasks.append({
        "task_id": task_id,
        "category": "e2e_task",
        "title": f"E2E 任务 {index}",
        "assignee": employee["display_name"],
        "priority": "high",
        "target": {"metric": "e2e_completion"},
        "reason": "Playwright 端到端测试任务",
        "evidence_required": ["图片证据", "脱敏单号"],
        "requires_approval": False,
        "due_at": due_at,
    })
task_store.save_plan({"date": today, "tasks": tasks}, "WH-LION-001")
fixtures["task_ids"] = [item["task_id"] for item in tasks]
fixtures_path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8")
`;
const seeded = spawnSync(python, ['-c', seedScript], {
  cwd: root,
  env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  encoding: 'utf-8'
});
if (seeded.status !== 0) {
  process.stderr.write(seeded.stdout || '');
  process.stderr.write(seeded.stderr || '');
  process.exit(seeded.status || 1);
}

const child = spawn(python, [
  'app_fastapi.py',
  '--host', '127.0.0.1',
  '--port', '8766',
  '--config', configPath,
  '--metrics-db', metricsDb,
  '--store-db', storeDb,
  '--chat-db', chatDb
], {
  cwd: root,
  env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  stdio: 'inherit'
});

let stopping = false;
function stop(signal) {
  if (stopping) return;
  stopping = true;
  child.kill(signal);
  setTimeout(() => process.exit(0), 1500).unref();
}
process.on('SIGINT', () => stop('SIGINT'));
process.on('SIGTERM', () => stop('SIGTERM'));
child.on('exit', (code, signal) => {
  if (signal) console.error(`E2E server exited with signal ${signal}`);
  process.exit(code ?? 1);
});

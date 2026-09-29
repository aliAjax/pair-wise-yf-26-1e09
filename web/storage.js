/* 存储整理页面接入：查看占用与残留实体、确认后清理。 */
(function () {
  const $ = (id) => document.getElementById(id);
  const userSel = $("user");
  const out = $("storageOut");

  function fmt(n) {
    return Number(n || 0).toLocaleString("zh-CN");
  }

  async function api(path, method = "GET") {
    const r = await fetch(path, {
      method,
      headers: { "X-User-Id": userSel.value },
    });
    return { status: r.status, body: await r.json() };
  }

  async function loadStorage() {
    const [{ status, body: overview}, orphansResp] = await Promise.all([
      api("/api/storage/overview"),
      api("/api/storage/orphans"),
    ]);
    if (status !== 200) {
      out.textContent = JSON.stringify(overview, null, 2);
      return;
    }
    const rows = [
      ["内容实体数（物理份数）", fmt(overview.entity_count)],
      ["物理占用字节", fmt(overview.entity_bytes)],
      ["版本/副本引用数（逻辑份数）", fmt(overview.reference_count)],
      ["逻辑占用字节（不去重）", fmt(overview.logical_bytes)],
      ["按摘要共用已节省字节", fmt(overview.shared_saved_bytes)],
      ["无引用残留实体数", fmt(overview.orphan_count)],
      ["残留可回收字节", fmt(overview.orphan_bytes)],
    ];
    let html = '<table border="1" cellpadding="6" style="border-collapse:collapse">';
    for (const [k, v] of rows) html += `<tr><td>${k}</td><td align="right">${v}</td></tr>`;
    html += "</table>";

    if (orphansResp.status === 200 && orphansResp.body.items.length) {
      html += `<p>残留实体（最多 ${orphansResp.body.limit} 条，确认后清空，被引用的实体不会删除）：</p><ul>`;
      for (const it of orphansResp.body.items) {
        html += `<li><code>${it.sha256.slice(0, 16)}…</code> ${fmt(it.size)} 字节</li>`;
      }
      html += "</ul>";
    } else if (orphansResp.status !== 200) {
      html += `<p>预览残留实体失败（${orphansResp.status}）：${
        (orphansResp.body.error && orphansResp.body.error.message) || ""
      }</p>`;
    } else {
      html += "<p>当前没有无引用的残留实体。</p>";
    }

    if (overview.jobs.length) {
      html += "<p>近期整理任务：</p><ul>";
      for (const j of overview.jobs) {
        html += `<li>#${j.id} ${j.created_at} ${j.actor_id}：清除 ${j.deleted_count} 个实体 / ${fmt(
          j.deleted_bytes
        )} 字节（清理前残留 ${j.orphan_count_before} 个）</li>`;
      }
      html += "</ul>";
    }
    out.innerHTML = html;
  }

  async function purgeStorage() {
    if (!confirm("确认清空所有无人引用的残留内容实体？被版本或副本引用的实体不会被删除。")) return;
    const { status, body } = await api("/api/storage/purge", "POST");
    if (status === 200) {
      alert(`已清除 ${body.deleted_count} 个残留实体，回收 ${fmt(body.deleted_bytes)} 字节`);
    } else {
      alert(`清理失败：${(body.error && body.error.message) || status}`);
    }
    loadStorage();
  }

  window.addEventListener("DOMContentLoaded", () => {
    $("btnStorageView").addEventListener("click", loadStorage);
    $("btnStoragePurge").addEventListener("click", purgeStorage);
  });
})();

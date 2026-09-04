# 小说 API 客户端契约

## 基础约定

- 前缀：`/api/novel`
- 请求体与响应体：JSON，UTF-8。
- 鉴权：使用 owner 或 internal Bearer token。
- 成功响应使用资源字段；任务 `status` 始终返回字符串枚举值。

## 错误响应

除 FastAPI 请求校验错误外，小说 API 的业务错误统一为：

```json
{
  "detail": {
    "code": "job_not_found",
    "message": "任务不存在"
  }
}
```

常见状态码：

| 状态码 | code | 含义 |
|---|---|---|
| 403 | `project_write_forbidden` | 无项目写入权限 |
| 403 | `project_admin_forbidden` | 无项目成员管理权限 |
| 404 | `project_not_found` | 项目不存在或当前身份不可见 |
| 404 | `chapter_not_found` | 章节不存在 |
| 404 | `job_not_found` | 生成任务不存在 |
| 409 | `project_version_conflict` | 乐观锁版本冲突 |
| 409 | `job_not_published` | 任务尚未发布，不能同步文件 |
| 409 | `file_sync_unavailable` | 缺少项目根目录或章节 |
| 500 | `file_sync_failed` | 数据库已更新，但文件同步失败，可重试 `/file-sync` |

## 资源字段

### Project

`project_id`, `name`, `slug`, `owner_id`, `root`, `metadata`, `version`, `updated_at`

### Chapter

`chapter_no`, `title`, `content`, `project_id`, `status`

### Draft

`project_id`, `chapter_no`, `content`, `version`, `status`

### GenerationJob

`job_id`, `idempotency_key`, `project_id`, `chapter_no`, `status`, `prompt`, `draft_content`, `review_result`, `error`, `attempts`, `progress`, `heartbeat_at`, `version`

## 幂等与并发

- 创建任务必须提供 `idempotency_key`；重复 key 返回同一任务。
- 项目和章节更新可提供 `expected_version`；版本不匹配返回 `409/project_version_conflict`。
- 发布会同时更新任务和章节；文件同步失败时数据库状态保留，客户端可调用 `POST /projects/{project_id}/jobs/{job_id}/file-sync` 重试。
- 任务状态迁移由仓储校验：`queued → generating → awaiting_confirmation → published`，失败可 `retry`，运行中/排队中可 `cancel`；非法迁移返回 `409/invalid_job_transition`。
- 运行器认领后通过 `POST /projects/{project_id}/jobs/{job_id}/heartbeat?progress=0..100` 更新进度与心跳；超时任务会重新排队。
- 章节搜索：`GET /projects/{project_id}/chapters/search?q=...&limit=50&offset=0`；索引管理：`POST /projects/{project_id}/index/rebuild`、`GET /projects/{project_id}/index/status`。
- 列表接口支持 `limit`/`offset` 的分页参数；审计还支持 `action`、`success` 过滤并返回 `total`。成员可通过 `DELETE /projects/{project_id}/members/{user_id}` 移除，但项目所有者受保护。
- MCP 提供同等的 `search_novel_chapters`、`retry_novel_job`、`cancel_novel_job`、`sync_novel_file_index` 和 `get_novel_index_status` 工具。

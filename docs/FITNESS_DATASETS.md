# 健身数据集与营养数据导入

本文档说明个人助手如何使用公开动作库和食品营养数据。项目只接收**本地文件导入**，不会在服务启动或运行期间联网下载第三方数据，也不会把第三方原始大数据、图片/GIF 和个人健康数据提交到 Git。

## 推荐数据源

| 用途 | 数据源 | 适合内容 | 许可证/注意事项 |
|---|---|---|---|
| 基础动作目录 | [yuhonas/free-exercise-db](https://github.com/yuhonas/free-exercise-db) | 动作名称、肌群、器械、说明、图片引用 | 仓库标注 Unlicense；导入前仍应检查当前仓库 LICENSE 和媒体文件授权。 |
| 动作元数据补充 | [wrkout/exercises.json](https://github.com/wrkout/exercises.json) | 动作 JSON/SQL 元数据 | 以该仓库当前 LICENSE 和具体文件说明为准；不要默认复制图片或动画媒体。 |
| 食品营养主库 | [USDA FoodData Central](https://fdc.nal.usda.gov/download-datasets.html) | 食材、热量、蛋白质、脂肪、碳水、膳食纤维、钠等 | 官方数据适合做营养基础库；导入时保留 USDA 署名和数据版本。 |
| 包装食品补充 | [Open Food Facts](https://github.com/openfoodfacts/openfoodfacts-server) | 条形码、品牌、配料、包装营养标签 | 数据采用 ODbL；图片和其他媒体可能有单独授权，不能把图片授权当成数据库授权。 |

项目不把 `ExerciseDB`、`wger` 或研究型知识图谱直接打包进仓库。它们可以用于比较字段和设计导入适配器，但原始数据、图片、代码许可证需要逐项审核。

## 动作库导入

下载或整理本地 JSON 后执行：

```bash
python server/scripts/import_fitness_catalog.py exercises.json \
  --source free-exercise-db \
  --license Unlicense \
  --attribution "free-exercise-db"
```

支持：

- 顶层数组：`[{"id": "...", "name": "..."}]`
- 包装对象：`{"exercises": [{"id": "...", "name": "..."}]}`
- Free Exercise DB 常见字段：`primaryMuscles`、`secondaryMuscles`、`equipment`、`instructions`、`images`

导入会按 `source + source_id` 幂等更新，保留 `license`、`attribution`、`created_at` 和 `updated_at`。动作图片只保存外部引用，不自动抓取。

## 食品营养导入

USDA 或 Open Food Facts 数据需要先下载到本地，再执行：

```bash
python server/scripts/import_fitness_foods.py foods.json \
  --source usda-fdc \
  --license CC0 \
  --attribution "USDA FoodData Central"
```

Open Food Facts 示例：

```bash
python server/scripts/import_fitness_foods.py products.json \
  --source open-food-facts \
  --license ODbL-1.0 \
  --attribution "Open Food Facts" \
  --skip-invalid
```

支持的常见 JSON 包装：

- 顶层数组；
- `{"foods": [...]}`：常见 USDA 数据结构；
- `{"products": [...]}`：常见 Open Food Facts 数据结构；
- `{"data": [...]}` 或 `{"results": [...]}`：便于处理经过筛选的本地导出。

食品营养值统一按**每 100 克**保存。记录饮食时提供实际克数，服务会计算：

- 热量 kcal；
- 蛋白质 g；
- 脂肪 g；
- 碳水 g；
- 膳食纤维 g；
- 钠 mg。

导入会按 `source + source_id` 幂等更新；无效记录默认使整批失败，只有显式传入 `--skip-invalid` 才会跳过并返回前 20 条错误摘要。

## API 使用边界

- `GET /api/fitness/foods`：搜索食品；
- `GET /api/fitness/foods/{id}`：查看食品和来源元数据；
- `POST /api/fitness/foods/import`：导入已准备好的食品记录；
- `POST /api/fitness/nutrition/logs`：记录一次摄入；
- `GET /api/fitness/nutrition/logs?date=YYYY-MM-DD`：查看当天记录；
- `GET /api/fitness/nutrition/summary?date=YYYY-MM-DD`：查看当天营养合计；
- `DELETE /api/fitness/nutrition/logs/{id}`：删除自己的饮食记录。

食品目录是公开数据，饮食记录是用户数据。目录记录保留来源信息，饮食记录按 `user_id` 隔离，并保存当时的食品名称、品牌和营养快照；以后更新目录不会篡改历史统计。

## 合规与安全要求

1. 不把原始 USDA/Open Food Facts 大文件提交到仓库；只提交导入器、测试夹具和来源说明。
2. 不在导入 JSON 中放入真实用户姓名、QQ 号、地址、订单号或其他个人信息。
3. 不把图片、GIF、视频下载到服务端；动作表只保存经过长度限制的引用。
4. 每次导入都显式记录来源、许可证、署名和内容哈希。
5. 营养计算用于记录和一般健身分析，不替代医生、注册营养师或疾病饮食方案。
6. 对过敏、慢性病、孕期、进食障碍、药物相互作用等问题，助手只能提示寻求专业意见，不自动给出医疗方案。

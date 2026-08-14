# 数据库迁移

新数据库执行：

```bash
flask --app app:create_app db upgrade
```

仅当数据库确实来自旧版 `db.create_all()`、已有旧表且尚无 Alembic 版本记录时，
先备份数据库，再执行：

```bash
flask --app app:create_app db stamp 20260811_01
flask --app app:create_app db upgrade
```

第二段迁移会把升级前已有的交易决策和净值快照标记为 `live`。旧版引擎默认直接连接实盘执行器，不能把这些历史记录误标为新增的模拟模式。

生产部署应在启动唯一应用实例前完成迁移。

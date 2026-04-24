# Mock 数据说明：配件库存实时监控、预测、优化与自动补货

| 模块 | 用途 | 主要关联工作表 |
|---|---|---|
| 实时库存监控 | 持续读取库存快照与事件流，计算可用量、周转天数、风险状态 | Inventory_RT, Real_Time_Events |
| 需求预测 | 用历史消耗、丢失需求、市场指数、检修周期生成未来 28 天预测 | Demand_History_120D, Market_Signals, Forecast_28D |
| 库存结构优化 | 根据关键等级、交期、目标服务水平、目标库存识别缺货/积压 | Inventory_RT, SKU_Master, Dashboard |
| 自动补充 | 对低风险高确定性缺口生成 Auto PO，对高价值/低置信度任务转人工审批 | Inventory_RT, Auto_Replenishment_Queue |
| 异常边界 | 停产件、供应商延期、市场冲击、慢动积压 | Test_Cases, Supplier_Performance |
| 建议验证顺序：1) 查看 Dashboard KPI；2) 在 Inventory_RT 修改 on_hand/reserved/inbound；3) 检查 risk_status 与 recommended_order_qty；4) 对照 Test_Cases 判断是否符合预期。 |  |  |

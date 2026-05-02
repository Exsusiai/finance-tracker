export default function SettingsPage() {
  return (
    <div className="min-h-screen bg-background text-foreground pb-16 md:pb-0">
      <div className="mx-auto max-w-4xl px-4 py-6 md:px-6 lg:px-8">
        <div className="mb-6">
          <h1 className="text-2xl font-bold tracking-tight">⚙️ 设置</h1>
          <p className="text-sm text-muted-foreground mt-1">
            管理账户偏好、分类、银行连接和数据导入
          </p>
        </div>

        <div className="rounded-xl border border-border bg-card p-12">
          <div className="flex flex-col items-center justify-center gap-4 text-center">
            <div className="h-16 w-16 rounded-full border-4 border-muted border-t-primary animate-spin" />
            <div>
              <p className="text-base font-medium text-foreground">设置功能开发中…</p>
              <p className="text-sm text-muted-foreground mt-1">
                即将上线：分类管理、银行账户配置、数据导出与备份
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

"""插件注册中心：初始化阶段只提供清单与隔离边界，不加载任何插件实现。

清单形状由 shared/schemas/plugin.schema.json 定型（pluginId / type / entrypoint /
dependencies / capabilities / schemaVersion），M1 载入具体插件时无需变更契约。
"""

from app.schemas import PluginList, PluginManifest

BUILTIN_PLUGINS: list[PluginManifest] = []


class PluginRegistry:
    """按项目返回已启用插件；M1 将插件放入独立子进程执行。"""

    def list_for_project(self, project_id: str) -> PluginList:
        return PluginList(
            items=[plugin for plugin in BUILTIN_PLUGINS if plugin.enabled],
            isolation="subprocess",
            note=(
                f"项目 {project_id} 暂无已启用插件。插件将以独立子进程运行，"
                "超时或崩溃不影响分析主任务。"
            ),
        )


def create_plugin_registry() -> PluginRegistry:
    return PluginRegistry()

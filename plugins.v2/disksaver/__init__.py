import datetime
import threading
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import (
    NotificationType,
    ServiceInfo,
)
from app.utils.system import SystemUtils
from app.helper.downloader import DownloaderHelper

lock = threading.Lock()


class diskSaver(_PluginBase):
    # 插件名称
    plugin_name = "diskSaver"
    # 插件描述
    plugin_desc = "监控路径所在磁盘剩余空间，低于指定值时自动限制qb下载速度"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/BrettDean/MoviePilot-Plugins/refs/heads/main/icons/disksaver.png"
    # 插件版本
    plugin_version = "1.0.2"
    # 插件作者
    plugin_author = "Dean"
    # 作者主页
    author_url = "https://github.com/BrettDean/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "diskSaver_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _downloaderSpeedLimit = 1
    _cron = None
    _size = 0
    _monitor_dirs = ""
    _min_free_space = 100
    _download_limit_old_val = 0
    _upload_limit_old_val = 0
    _dl_speed_limited = False
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[Path]] = {}
    # 存储源目录转移方式
    _transferconf: Dict[str, Optional[str]] = {}
    _overwrite_mode: Dict[str, Optional[str]] = {}
    _medias = {}
    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        self.downloader_helper = DownloaderHelper()
        # 清空配置
        self._dirconf = {}
        self._transferconf = {}
        self._overwrite_mode = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._cron = config.get("cron") or "*/10 * * * *"
            self._size = config.get("size") or 0
            self._downloaderSpeedLimit = config.get("downloaderSpeedLimit") or 1
            self._downloaders = config.get("downloaders") or ["不限速-diskSaver"]
            self._min_free_space = config.get("min_free_space") or 100
            self._download_limit_old_val = config.get("download_limit_old_val")
            self._upload_limit_old_val = config.get("upload_limit_old_val")
            self._dl_speed_limited = config.get("dl_speed_limited")

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 读取目录配置
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                # 格式源目录:目的目录
                if not mon_path:
                    continue

                # 自定义覆盖方式
                _overwrite_mode = "never"
                if mon_path.count("@") == 1:
                    _overwrite_mode = mon_path.split("@")[1]
                    mon_path = mon_path.split("@")[0]

                mon_path = mon_path.split("#")[0]

                # 存储目的目录
                if SystemUtils.is_windows():
                    if mon_path.count(":") > 1:
                        paths = [
                            mon_path.split(":")[0] + ":" + mon_path.split(":")[1],
                            mon_path.split(":")[2] + ":" + mon_path.split(":")[3],
                        ]
                    else:
                        paths = [mon_path]
                else:
                    paths = mon_path.split(":")

                # 目的目录
                target_path = None
                if len(paths) > 1:
                    mon_path = paths[0]
                    target_path = Path(paths[1])
                    self._dirconf[mon_path] = target_path
                else:
                    self._dirconf[mon_path] = None

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("diskSaver立即运行一次")
                self._scheduler.add_job(
                    name="diskSaver手动运行",
                    func=self.monitor_disk,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3),
                )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "monitor_dirs": self._monitor_dirs,
                "size": self._size,
                "cron": self._cron,
                "downloaderSpeedLimit": self._downloaderSpeedLimit,
                "downloaders": self._downloaders,
                "min_free_space": self._min_free_space,
                "download_limit_old_val": self._download_limit_old_val,
                "upload_limit_old_val": self._upload_limit_old_val,
                "dl_speed_limited": self._dl_speed_limited,
            }
        )

    @property
    def service_info(self) -> Optional[ServiceInfo]:
        """
        服务信息
        """
        if not self._downloaders:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        services = self.downloader_helper.get_services(name_filters=self._downloaders)

        if not services:
            logger.warning("获取下载器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"下载器 {service_name} 未连接，请检查配置")
            elif not self.check_is_qb(service_info):
                logger.warning(
                    f"不支持的下载器类型 {service_name}，仅支持QB，请检查配置"
                )
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning("没有已连接的下载器，请检查配置")
            return None

        return active_services

    def get_downloader_limit_current_val(self):
        for service in self.service_info.values():
            downloader_name = service.name
            downloader_obj = service.instance
            if not downloader_obj:
                logger.error(f"获取下载器失败 {downloader_name}")
                continue
            download_limit_current_val, upload_limit_current_val = (
                downloader_obj.get_speed_limit()
            )
        return download_limit_current_val, upload_limit_current_val

    def set_download_limit(self, download_limit):

        if not download_limit or not download_limit.isdigit():
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="diskSaver QB限速失败",
                text="download_limit不是一个数值",
            )
            return False

        flag = True
        for service in self.service_info.values():
            downloader_name = service.name
            downloader_obj = service.instance
            if not downloader_obj:
                logger.error(f"获取下载器失败 {downloader_name}")
                continue
            _, upload_limit_current_val = downloader_obj.get_speed_limit()
            flag = flag and downloader_obj.set_speed_limit(
                download_limit=int(download_limit),
                upload_limit=int(upload_limit_current_val),
            )
        return flag

    def check_is_qb(self, service_info) -> bool:
        """
        检查下载器类型是否为 qbittorrent 或 transmission
        """
        if self.downloader_helper.is_downloader(
            service_type="qbittorrent", service=service_info
        ):
            return True
        elif self.downloader_helper.is_downloader(
            service_type="transmission", service=service_info
        ):
            return False
        return False

    def human_readable_bytes(self, byte_size: int) -> str:
        """
        将字节转换为更适合阅读的单位（KiB, MiB, GiB, TiB等）
        """
        for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
            if byte_size < 1024.0:
                return f"{byte_size:.2f} {unit}"
            byte_size /= 1024.0

    def monitor_disk(self):
        try:
            # 所有目录路径放入一个列表
            paths_to_process = list(self._dirconf.keys())
            # 如果某个路径不存在，则返回
            for path in paths_to_process:
                file_path = Path(path)
                if not file_path.exists():
                    logger.error(f"监控目录 {path} 不存在，请检查配置")
                    return
            logger.info(f"开始获取目录所在磁盘剩余空间 {paths_to_process} ...")
            # 全程加锁
            with lock:
                # 将 paths_to_process 转换为 Path 对象列表
                paths = [Path(path) for path in paths_to_process]
                storage_usage = self.usage(paths)
                available = storage_usage.available

                if self._downloaderSpeedLimit == 0:
                    logger.debug("下载器限速为 0，不限速")
                elif not self._downloaders or "不限速-diskSaver" in self._downloaders:
                    logger.debug(
                        "已勾选不限速，或未勾选，或没有下载器，不进行下载器限速"
                    )
                elif (
                    available < int(self._min_free_space) * (2**30)
                    and not self._dl_speed_limited
                ):
                    logger.info(
                        f"开始限速 - 磁盘剩余空间 {self._min_free_space} GiB 小于 设置的最小空间 {self.human_readable_bytes(available)}"
                    )
                    # 先获取当前下载器的限速
                    download_limit_current_val, upload_limit_current_val = (
                        self.get_downloader_limit_current_val()
                    )
                    # 现在的qb限速数值大于手动设置的数值才有必要限速，否则限速反而是提速了
                    if float(download_limit_current_val) > float(
                        self._downloaderSpeedLimit
                    ):
                        # 下载器限速
                        self.set_download_limit(str(self._downloaderSpeedLimit))

                    # 标记已限速(_dl_speed_limited)并更新_download_limit_old_val和_upload_limit_old_val
                    self._dl_speed_limited = True
                    self._download_limit_old_val = download_limit_current_val
                    self._upload_limit_old_val = upload_limit_current_val
                    self.__update_config()
                elif (
                    available >= int(self._min_free_space) * (2**30)
                    and self._dl_speed_limited
                ):
                    logger.info(
                        f"取消限速 - 磁盘剩余空间 {self._min_free_space} GiB 大于 设置的最小空间 {self.human_readable_bytes(available)}"
                    )
                    # 先获取当前下载器的限速
                    download_limit_current_val, upload_limit_current_val = (
                        self.get_downloader_limit_current_val()
                    )
                    # 现在的qb限速数值小于autoTransfer限速之前记录的数值才有必要恢复(提速)，否则提速反而是降(限)速了
                    if float(download_limit_current_val) < float(
                        self._download_limit_old_val
                    ):
                        # 下载器恢复原限速
                        self.set_download_limit(str(self._download_limit_old_val))
                    self._dl_speed_limited = False
                    self.__update_config()

        except Exception as e:
            logger.error(
                "diskSaver 监控出错：%s - %s" % (str(e), traceback.format_exc())
            )

    def usage(self, paths: List[Path]) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        :param paths: 传入需要计算存储使用情况的目录路径列表
        """
        # 获取传入的路径列表，计算总存储和空闲存储
        total_storage, free_storage = SystemUtils.space_usage(paths)

        # 返回计算的存储使用情况
        return schemas.StorageUsage(
            total=total_storage, available=free_storage  # 返回总存储  # 返回可用存储
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled:
            return [
                {
                    "id": "diskSaver",
                    "name": "监控路径所在磁盘剩余空间，低于指定值时自动限制qb下载速度",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.monitor_disk,
                    "kwargs": {},
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VForm",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 6},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "enabled",
                                                    "label": "启用插件",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 6},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "onlyonce",
                                                    "label": "立即运行一次",
                                                },
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12},
                                        "content": [
                                            {
                                                "component": "VProgressLinear",
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 3, "md": 3},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "*/10 * * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 4, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_free_space",
                                            "label": "最小剩余空间, 默认100, 单位GiB",
                                            "placeholder": "100",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12},
                                        "content": [
                                            {
                                                "component": "VProgressLinear",
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "downloaders",
                                            "label": "要限速的下载器",
                                            "items": [
                                                {
                                                    "title": "不限速(勾选此项或留空默认不限速)",
                                                    "value": "不限速-diskSaver",
                                                },
                                                *[
                                                    {
                                                        "title": config.name,
                                                        "value": config.name,
                                                    }
                                                    for config in self.downloader_helper.get_configs().values()
                                                    if config.type == "qbittorrent"
                                                ],
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 3, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "downloaderSpeedLimit",
                                            "label": "转下载器限速(KiB/s)",
                                            "placeholder": "0或留空不限速,推荐1",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12},
                                        "content": [
                                            {
                                                "component": "VProgressLinear",
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "monitor_dirs",
                                            "label": "监控目录",
                                            "rows": 2,
                                            "auto-grow": "{{ monitor_dirs.length > 0 }}",
                                            "placeholder": "每一行一个目录\n",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "监控路径所在磁盘剩余空间，低于指定值时自动限制qb下载速度",
                                            "style": {
                                                "white-space": "pre-line",
                                                "word-wrap": "break-word",
                                                "height": "auto",
                                                "max-height": "300px",
                                                "overflow-y": "auto",
                                            },
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "monitor_dirs": "",
            "cron": "*/10 * * * *",
            "size": 0,
            "downloaderSpeedLimit": 1,
            "downloaders": "不限速-diskSaver",
            "min_free_space": 100,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None

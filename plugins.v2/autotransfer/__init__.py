import datetime
import re
import shutil
import os
import threading
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.plugins import _PluginBase
from app.schemas import (
    NotificationType,
    TransferInfo,
    TransferDirectoryConf,
    ServiceInfo,
)
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils
from app.helper.downloader import DownloaderHelper

lock = threading.Lock()


class autoTransfer(_PluginBase):
    # 插件名称
    plugin_name = "autoTransfer"
    # 插件描述
    plugin_desc = "类似v1的目录监控，可定期整理文件"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/BrettDean/MoviePilot-Plugins/refs/heads/main/icons/autotransfer.png"
    # 插件版本
    plugin_version = "1.0.19"
    # 插件作者
    plugin_author = "Dean"
    # 作者主页
    author_url = "https://github.com/BrettDean/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "autoTransfer_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    transferhis = None
    downloadhis = None
    transferchian = None
    tmdbchain = None
    storagechain = None
    _observer = []
    _enabled = False
    _notify = False
    _onlyonce = False
    _history = False
    _scrape = False
    _category = False
    _refresh = False
    _softlink = False
    _strm = False
    _del_empty_dir = False
    _downloaderSpeedLimit = 0
    _pathAfterMoveFailure = None
    _cron = None
    filetransfer = None
    mediaChain = None
    _size = 0
    # 取消限速开关
    _pre_cancel_speed_limit = False
    # 转移方式
    _transfer_type = "move"
    _monitor_dirs = ""
    _exclude_keywords = ""
    _interval: int = 10
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[Path]] = {}
    # 存储源目录转移方式
    _transferconf: Dict[str, Optional[str]] = {}
    _overwrite_mode: Dict[str, Optional[str]] = {}
    _medias = {}
    # 退出事件
    _event = threading.Event()
    _move_failed_files = True
    _move_excluded_files = True

    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper()
        self.downloadhis = DownloadHistoryOper()
        self.transferchian = TransferChain()
        self.tmdbchain = TmdbChain()
        self.mediaChain = MediaChain()
        self.storagechain = StorageChain()
        self.filetransfer = FileManagerModule()
        self.downloader_helper = DownloaderHelper()
        # 清空配置
        self._dirconf = {}
        self._transferconf = {}
        self._overwrite_mode = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._history = config.get("history")
            self._scrape = config.get("scrape")
            self._category = config.get("category")
            self._refresh = config.get("refresh")
            self._transfer_type = config.get("transfer_type")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._interval = config.get("interval") or 10
            self._cron = config.get("cron") or "*/10 * * * *"
            self._size = config.get("size") or 0
            self._softlink = config.get("softlink")
            self._strm = config.get("strm")
            self._del_empty_dir = config.get("del_empty_dir") or False
            self._pathAfterMoveFailure = config.get("pathAfterMoveFailure") or None
            self._downloaderSpeedLimit = config.get("downloaderSpeedLimit") or 0
            self._downloaders = config.get("downloaders") or ["不限速-autoTransfer"]
            self._move_failed_files = config.get("move_failed_files", True)
            self._move_excluded_files = config.get("move_excluded_files", True)
            self._pre_cancel_speed_limit = config.get("pre_cancel_speed_limit", False)

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger="interval", seconds=15)

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

                # 自定义转移方式
                _transfer_type = self._transfer_type
                if mon_path.count("#") == 1:
                    _transfer_type = mon_path.split("#")[1]
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

                # 转移方式
                self._transferconf[mon_path] = _transfer_type
                self._overwrite_mode[mon_path] = _overwrite_mode

                if self._enabled:
                    # 检查媒体库目录是不是下载目录的子目录
                    try:
                        if target_path and target_path.is_relative_to(Path(mon_path)):
                            logger.warn(
                                f"目的目录:{target_path} 是源目录: {mon_path} 的子目录，无法整理"
                            )
                            self.systemmessage.put(
                                f"目的目录:{target_path} 是源目录: {mon_path} 的子目录，无法整理",
                            )
                            continue
                    except Exception as e:
                        logger.debug(str(e))

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("autotransfer整理文件，立即运行一次")
                self._scheduler.add_job(
                    name="autotransfer整理文件",
                    func=self.main,
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
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "transfer_type": self._transfer_type,
                "monitor_dirs": self._monitor_dirs,
                "exclude_keywords": self._exclude_keywords,
                "interval": self._interval,
                "history": self._history,
                "softlink": self._softlink,
                "strm": self._strm,
                "scrape": self._scrape,
                "category": self._category,
                "size": self._size,
                "refresh": self._refresh,
                "cron": self._cron,
                "del_empty_dir": self._del_empty_dir,
                "pathAfterMoveFailure": self._pathAfterMoveFailure,
                "downloaderSpeedLimit": self._downloaderSpeedLimit,
                "downloaders": self._downloaders,
                "move_failed_files": self._move_failed_files,
                "move_excluded_files": self._move_excluded_files,
                "pre_cancel_speed_limit": self._pre_cancel_speed_limit,
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

    def set_download_limit(self, download_limit):
        try:
            try:
                download_limit = int(download_limit)
            except Exception as e:
                logger.error(
                    f"download_limit 转换失败 {str(e)}, traceback={traceback.format_exc()}"
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
        except Exception as e:
            logger.error(
                f"设置下载限速失败 {str(e)}, traceback={traceback.format_exc()}"
            )
            return False

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

    def get_downloader_limit_current_val(self):
        """
        获取下载器当前的下载限速和上传限速

        :return: tuple of (download_limit_current_val, upload_limit_current_val)
        """
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

    def moveFailedFilesToPath(self, fail_reason, src):
        """
        转移失败的文件到指定的路径

        :param fail_reason: 失败的原因
        :param src: 需要转移的文件路径
        """
        is_download_speed_limited = False
        try:
            # 先获取当前下载器的限速
            download_limit_current_val, _ = self.get_downloader_limit_current_val()
            if (
                float(download_limit_current_val) > float(self._downloaderSpeedLimit)
                or float(download_limit_current_val) == 0
            ):
                is_download_speed_limited = self.set_download_limit(
                    self._downloaderSpeedLimit
                )
                if is_download_speed_limited:
                    logger.info(
                        f"下载器限速成功设置为 {self._downloaderSpeedLimit} KiB/s"
                    )
                else:
                    logger.info(
                        f"下载器限速失败，请检查下载器 {', '.join(self._downloaders)} 的联通性，本次整理将跳过下载器限速"
                    )
            else:
                logger.info(
                    f"不用设置下载器限速，当前下载器限速为 {download_limit_current_val} KiB/s 大于或等于设定值 {self._downloaderSpeedLimit} KiB/s"
                )
        except Exception as e:
            logger.error(
                f"下载器限速失败，请检查下载器 {', '.join(self._downloaders)} 的联通性，本次整理将跳过下载器限速"
            )
            logger.debug(
                f"下载器限速失败：{str(e)}, traceback={traceback.format_exc()}"
            )
            is_download_speed_limited = False

        try:
            logger.info(f"开始转移失败的文件 '{src}'")
            dst = self._pathAfterMoveFailure
            if dst[-1] == "/":
                dst = dst[:-1]
            new_dst = f"{dst}/{fail_reason}{src}"
            new_dst_dir = os.path.dirname(f"{dst}/{fail_reason}{src}")
            os.makedirs(new_dst_dir, exist_ok=True)
            # 检查是否有重名文件
            if os.path.exists(new_dst):
                timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                filename, ext = os.path.splitext(new_dst)
                new_dst = f"{filename}_{timestamp}{ext}"
            shutil.move(src, new_dst)
            logger.info(f"成功移动转移失败的文件 '{src}' 到 '{new_dst}'")
        except Exception as e:
            logger.error(
                f"将转移失败的文件 '{src}' 移动到 '{new_dst}' 失败, traceback={traceback.format_exc()}"
            )

        # 恢复原速
        if is_download_speed_limited:
            recover_download_limit_success = self.set_download_limit(
                download_limit_current_val
            )
            if recover_download_limit_success:
                logger.info("取消下载器限速成功")
            else:
                logger.error("取消下载器限速失败")

    def main(self):
        """
        立即运行一次
        """
        try:
            logger.info(f"插件{self.plugin_name} V{self.plugin_version} 开始运行")
            # 执行前先取消下载器限速
            if self._pre_cancel_speed_limit:
                logger.info("【预取消限速】正在取消下载器限速...")
                if self.set_download_limit(0):
                    logger.info("下载器限速已取消")
                else:
                    logger.error("下载器限速取消失败")

            # 遍历所有目录
            for mon_path in self._dirconf.keys():
                logger.info(f"开始处理目录 {mon_path} ...")
                list_files = SystemUtils.list_files(
                    directory=Path(mon_path),
                    extensions=settings.RMT_MEDIAEXT,
                    min_filesize=int(self._size),
                    recursive=True,
                )
                logger.info(f"源目录 {mon_path} 共发现 {len(list_files)} 个视频")
                unique_items = {}

                # 遍历目录下所有文件
                for file_path in list_files:
                    logger.info(f"开始处理文件 {file_path} ...")
                    transferinfo, mediainfo, file_meta = self.__handle_file(
                        event_path=str(file_path), mon_path=mon_path
                    )

                    unique_key = Path(transferinfo.target_diritem.path)

                    # 存储不重复的项
                    if unique_key not in unique_items:
                        unique_items[unique_key] = (transferinfo, mediainfo, file_meta)

                # 刮削
                if self._scrape:
                    for transferinfo, mediainfo, file_meta in unique_items.values():
                        self.mediaChain.scrape_metadata(
                            fileitem=transferinfo.target_diritem,
                            meta=file_meta,
                            mediainfo=mediainfo,
                        )

            logger.info("目录内所有文件整理完成！")
        except Exception as e:
            logger.error(
                f"插件{self.plugin_name} V{self.plugin_version} 运行失败，错误信息:{e}，traceback={traceback.format_exc()}"
            )

    def __handle_file(self, event_path: str, mon_path: str):
        """
        同步一个文件
        :param event_path: 事件文件路径
        :param mon_path: 监控目录
        """
        file_path = Path(event_path)
        try:
            if not file_path.exists():
                return
            # 全程加锁
            with lock:
                transfer_history = self.transferhis.get_by_src(event_path)
                if transfer_history:
                    logger.info("文件已处理过：%s" % event_path)
                    return

                # 回收站及隐藏的文件不处理
                if (
                    event_path.find("/@Recycle/") != -1
                    or event_path.find("/#recycle/") != -1
                    or event_path.find("/.") != -1
                    or event_path.find("/@eaDir") != -1
                ):
                    logger.debug(f"{event_path} 是回收站或隐藏的文件")
                    return

                # 命中过滤关键字不处理
                if self._exclude_keywords:
                    for keyword in self._exclude_keywords.split("\n"):
                        if keyword and re.findall(keyword, event_path):
                            logger.info(
                                f"{event_path} 命中过滤关键字 {keyword}，不处理"
                            )
                            if (
                                self._pathAfterMoveFailure is not None
                                and self._transfer_type == "move"
                                and self._move_excluded_files
                            ):
                                self.moveFailedFilesToPath(
                                    "命中过滤关键字", str(file_path)
                                )
                            return

                # 整理屏蔽词不处理
                transfer_exclude_words = self.systemconfig.get(
                    SystemConfigKey.TransferExcludeWords
                )
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(
                            r"%s" % keyword, event_path, re.IGNORECASE
                        ):
                            logger.info(
                                f"{event_path} 命中整理屏蔽词 {keyword}，不处理"
                            )
                            if (
                                self._pathAfterMoveFailure is not None
                                and self._transfer_type == "move"
                                and self._move_excluded_files
                            ):
                                self.moveFailedFilesToPath(
                                    "命中整理屏蔽词", str(file_path)
                                )
                            return

                # 不是媒体文件不处理
                if file_path.suffix not in settings.RMT_MEDIAEXT:
                    logger.debug(f"{event_path} 不是媒体文件")
                    return

                # 判断是不是蓝光目录
                if re.search(r"BDMV[/\\]STREAM", event_path, re.IGNORECASE):
                    # 截取BDMV前面的路径
                    blurray_dir = event_path[: event_path.find("BDMV")]
                    file_path = Path(blurray_dir)
                    logger.info(
                        f"{event_path} 是蓝光目录，更正文件路径为：{str(file_path)}"
                    )
                    # 查询历史记录，已转移的不处理
                    if self.transferhis.get_by_src(str(file_path)):
                        logger.info(f"{file_path} 已整理过")
                        return

                # 元数据
                file_meta = MetaInfoPath(file_path)
                if not file_meta.name:
                    logger.error(f"{file_path.name} 无法识别有效信息")
                    return

                # 判断文件大小
                if (
                    self._size
                    and float(self._size) > 0
                    and file_path.stat().st_size < float(self._size) * 1024**3
                ):
                    logger.info(f"{file_path} 文件大小小于监控文件大小，不处理")
                    return

                # 查询转移目的目录
                target: Path = self._dirconf.get(mon_path)
                # 查询转移方式
                transfer_type = self._transferconf.get(mon_path)

                # 查找这个文件项
                file_item = self.storagechain.get_file_item(
                    storage="local", path=file_path
                )
                if not file_item:
                    logger.warn(f"{event_path.name} 未找到对应的文件")
                    return
                # 识别媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta)
                if not mediainfo:
                    logger.warn(f"未识别到媒体信息，标题：{file_meta.name}")
                    # 新增转移成功历史记录
                    his = self.transferhis.add_fail(
                        fileitem=file_item, mode=transfer_type, meta=file_meta
                    )
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.Manual,
                            title=f"{file_path.name} 未识别到媒体信息，无法入库！\n"
                            f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别转移。",
                        )
                        # 转移失败文件到指定目录
                        if (
                            self._pathAfterMoveFailure is not None
                            and self._transfer_type == "move"
                            and self._move_failed_files
                        ):
                            self.moveFailedFilesToPath(
                                "未识别到媒体信息", file_item.path
                            )
                    return

                # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
                if not settings.SCRAP_FOLLOW_TMDB:
                    transfer_history = self.transferhis.get_by_type_tmdbid(
                        tmdbid=mediainfo.tmdb_id, mtype=mediainfo.type.value
                    )
                    if transfer_history:
                        mediainfo.title = transfer_history.title
                logger.info(
                    f"{file_path.name} 识别为：{mediainfo.type.value} {mediainfo.title_year}"
                )

                # 获取集数据
                if mediainfo.type == MediaType.TV:
                    episodes_info = self.tmdbchain.tmdb_episodes(
                        tmdbid=mediainfo.tmdb_id,
                        season=(
                            1
                            if file_meta.begin_season is None
                            else file_meta.begin_season
                        ),
                    )
                else:
                    episodes_info = None

                # 查询转移目的目录
                target_dir = DirectoryHelper().get_dir(
                    mediainfo, src_path=Path(mon_path)
                )
                if (
                    not target_dir
                    or not target_dir.library_path
                    or not target_dir.download_path.startswith(mon_path)
                ):
                    target_dir = TransferDirectoryConf()
                    target_dir.library_path = target
                    target_dir.transfer_type = transfer_type
                    target_dir.scraping = self._scrape
                    target_dir.renaming = True
                    target_dir.notify = False
                    target_dir.overwrite_mode = (
                        self._overwrite_mode.get(mon_path) or "never"
                    )
                    target_dir.library_storage = "local"
                    target_dir.library_category_folder = self._category
                else:
                    target_dir.transfer_type = transfer_type
                    target_dir.scraping = self._scrape

                if not target_dir.library_path:
                    logger.error(f"未配置源目录 {mon_path} 的目的目录")
                    return

                # 下载器限速
                is_download_speed_limited = False
                if (
                    target_dir.transfer_type
                    in [
                        "move",
                        "copy",
                        "rclone_copy",
                        "rclone_move",
                    ]
                    and "不限速-autoTransfer" not in self._downloaders
                    and self._downloaderSpeedLimit != 0
                ):
                    logger.info(
                        f"下载器限速 - {', '.join(self._downloaders)}，下载速度限制为 {self._downloaderSpeedLimit} KiB/s，因正在移动或复制文件{file_item.path}"
                    )
                    try:
                        # 先获取当前下载器的限速
                        download_limit_current_val, _ = (
                            self.get_downloader_limit_current_val()
                        )
                        if (
                            float(download_limit_current_val)
                            > float(self._downloaderSpeedLimit)
                            or float(download_limit_current_val) == 0
                        ):
                            is_download_speed_limited = self.set_download_limit(
                                self._downloaderSpeedLimit
                            )
                        else:
                            logger.info(
                                f"不用设置下载器限速，当前下载器限速为 {download_limit_current_val} KiB/s 大于或等于设定值 {self._downloaderSpeedLimit} KiB/s"
                            )
                    except Exception as e:
                        logger.error(
                            f"下载器限速失败，请检查下载器 {', '.join(self._downloaders)} 的联通性，本次整理将跳过下载器限速"
                        )
                        logger.debug(
                            f"下载器限速失败：{str(e)}, traceback={traceback.format_exc()}"
                        )
                        is_download_speed_limited = False

                    if not is_download_speed_limited:
                        logger.debug(f"下载器{', '.join(self._downloaders)} 限速失败")
                else:
                    if "不限速-autoTransfer" in self._downloaders:
                        log_msg = "已勾选'不限速'或勾选需限速的下载器，默认关闭限速"
                    elif self._downloaderSpeedLimit == 0:
                        log_msg = "下载速度限制为0或为空，默认关闭限速"
                    elif target_dir.transfer_type not in [
                        "move",
                        "copy",
                        "rclone_copy",
                        "rclone_move",
                    ]:
                        log_msg = "转移方式不是移动或复制，下载器限速默认关闭"
                    logger.info(log_msg)

                # 转移文件
                transferinfo: TransferInfo = self.chain.transfer(
                    fileitem=file_item,
                    meta=file_meta,
                    mediainfo=mediainfo,
                    target_directory=target_dir,
                    episodes_info=episodes_info,
                )
                # 恢复原速
                if is_download_speed_limited:
                    recover_download_limit_success = self.set_download_limit(
                        download_limit_current_val
                    )
                    if recover_download_limit_success:
                        logger.info("取消下载器限速成功")
                    else:
                        logger.error("取消下载器限速失败")

                if not transferinfo:
                    logger.error("文件转移模块运行失败")
                    return

                if not transferinfo.success:
                    # 转移失败
                    logger.warn(f"{file_path.name} 入库失败：{transferinfo.message}")

                    if self._history:
                        # 新增转移失败历史记录
                        self.transferhis.add_fail(
                            fileitem=file_item,
                            mode=transfer_type,
                            meta=file_meta,
                            mediainfo=mediainfo,
                            transferinfo=transferinfo,
                        )
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.Manual,
                            title=f"{mediainfo.title_year}{file_meta.season_episode} 入库失败！",
                            text=f"原因：{transferinfo.message or '未知'}",
                            image=mediainfo.get_message_image(),
                        )
                    # 转移失败文件到指定目录
                    if (
                        self._pathAfterMoveFailure is not None
                        and self._transfer_type == "move"
                        and self._move_failed_files
                    ):
                        self.moveFailedFilesToPath(transferinfo.message, file_item.path)
                    return

                if self._history:
                    # 新增转移成功历史记录
                    self.transferhis.add_success(
                        fileitem=file_item,
                        mode=transfer_type,
                        meta=file_meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo,
                    )

                if self._notify:
                    # 发送消息汇总
                    media_list = (
                        self._medias.get(mediainfo.title_year + " " + file_meta.season)
                        or {}
                    )
                    if media_list:
                        media_files = media_list.get("files") or []
                        if media_files:
                            file_exists = False
                            for file in media_files:
                                if str(file_path) == file.get("path"):
                                    file_exists = True
                                    break
                            if not file_exists:
                                media_files.append(
                                    {
                                        "path": str(file_path),
                                        "mediainfo": mediainfo,
                                        "file_meta": file_meta,
                                        "transferinfo": transferinfo,
                                    }
                                )
                        else:
                            media_files = [
                                {
                                    "path": str(file_path),
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo,
                                }
                            ]
                        media_list = {
                            "files": media_files,
                            "time": datetime.datetime.now(),
                        }
                    else:
                        media_list = {
                            "files": [
                                {
                                    "path": str(file_path),
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo,
                                }
                            ],
                            "time": datetime.datetime.now(),
                        }
                    self._medias[mediainfo.title_year + " " + file_meta.season] = (
                        media_list
                    )

                if self._refresh:
                    # 广播事件
                    self.eventmanager.send_event(
                        EventType.TransferComplete,
                        {
                            "meta": file_meta,
                            "mediainfo": mediainfo,
                            "transferinfo": transferinfo,
                        },
                    )

                if self._softlink:
                    # 通知实时软连接生成
                    self.eventmanager.send_event(
                        EventType.PluginAction,
                        {
                            "file_path": str(transferinfo.target_item.path),
                            "action": "softlink_file",
                        },
                    )

                if self._strm:
                    # 通知Strm助手生成
                    self.eventmanager.send_event(
                        EventType.PluginAction,
                        {
                            "file_path": str(transferinfo.target_item.path),
                            "action": "cloudstrm_file",
                        },
                    )

                # 移动模式删除空目录
                if transfer_type == "move" and self._del_empty_dir:
                    for file_dir in file_path.parents:
                        if len(str(file_dir)) <= len(str(Path(mon_path))):
                            # 重要，删除到监控目录为止
                            break
                        files = SystemUtils.list_files(
                            file_dir, settings.RMT_MEDIAEXT + settings.DOWNLOAD_TMPEXT
                        )
                        if not files:
                            logger.warn(f"移动模式，删除空目录：{file_dir}")
                            shutil.rmtree(file_dir, ignore_errors=True)

                # 返回成功的文件
                return transferinfo, mediainfo, file_meta

        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._medias or not self._medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._medias.keys()):
            media_list = self._medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            transferinfo = media_files[0].get("transferinfo")
            file_meta = media_files[0].get("file_meta")
            mediainfo = media_files[0].get("mediainfo")
            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.datetime.now() - last_update_time).total_seconds() > int(
                self._interval
            ) or mediainfo.type == MediaType.MOVIE:
                # 发送通知
                if self._notify:

                    # 汇总处理文件总大小
                    total_size = 0
                    file_count = 0

                    # 剧集汇总
                    episodes = []
                    for file in media_files:
                        transferinfo = file.get("transferinfo")
                        total_size += transferinfo.total_size
                        file_count += 1

                        file_meta = file.get("file_meta")
                        if file_meta and file_meta.begin_episode:
                            episodes.append(file_meta.begin_episode)

                    transferinfo.total_size = total_size
                    # 汇总处理文件数量
                    transferinfo.file_count = file_count

                    # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                    season_episode = None
                    # 处理文件多，说明是剧集，显示季入库消息
                    if mediainfo.type == MediaType.TV:
                        # 季集文本
                        season_episode = (
                            f"{file_meta.season} {StringUtils.format_ep(episodes)}"
                        )
                    # 发送消息
                    self.transferchian.send_transfer_message(
                        meta=file_meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo,
                        season_episode=season_episode,
                    )
                # 发送完消息，移出key
                del self._medias[medis_title_year_season]
                continue

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
                    "id": "autoTransfer",
                    "name": "类似v1的目录监控，可定期整理文件",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.main,
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
                                        "props": {"cols": 12, "md": 3},
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
                                        "props": {"cols": 12, "md": 3},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "notify",
                                                    "label": "发送通知",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 3},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "refresh",
                                                    "label": "刷新媒体库",
                                                },
                                            }
                                        ],
                                    },
                                ],
                            },
                            {
                                "component": "VForm",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "history",
                                                            "label": "存储历史记录",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "scrape",
                                                            "label": "是否刮削",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "category",
                                                            "label": "是否二级分类",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VForm",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "del_empty_dir",
                                                            "label": "删除空目录",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "softlink",
                                                            "label": "软连接",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "strm",
                                                            "label": "联动Strm生成",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 3},
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
                                "props": {"cols": 3, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "size",
                                            "label": "最低整理大小, 默认0, 单位MiB",
                                            "placeholder": "0",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 3, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "transfer_type",
                                            "label": "转移方式",
                                            "items": [
                                                {"title": "移动", "value": "move"},
                                                {"title": "复制", "value": "copy"},
                                                {"title": "硬链接", "value": "link"},
                                                {
                                                    "title": "软链接",
                                                    "value": "softlink",
                                                },
                                                {
                                                    "title": "Rclone复制",
                                                    "value": "rclone_copy",
                                                },
                                                {
                                                    "title": "Rclone移动",
                                                    "value": "rclone_move",
                                                },
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
                                            "model": "interval",
                                            "label": "入库消息延迟",
                                            "placeholder": "10",
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
                                            "label": "选择转移时要限速的下载器",
                                            "items": [
                                                {
                                                    "title": "不限速(勾选此项或留空默认不限速)",
                                                    "value": "不限速-autoTransfer",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "downloaderSpeedLimit",
                                            "label": "转移时下载器限速(KiB/s)",
                                            "placeholder": "0或留空不限速",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "pre_cancel_speed_limit",
                                            "label": "每次运行前取消qb限速",
                                            "hint": "每次运行插件前强制取消下载器限速（防止意外断电后限速未恢复）",
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
                                        "component": "VCol",
                                        "props": {"cols": 12},
                                        "content": [
                                            {
                                                "component": "VTextarea",
                                                "props": {
                                                    "model": "monitor_dirs",
                                                    "label": "监控目录(下载目录/源目录)",
                                                    "rows": 6,
                                                    "auto-grow": "{{ monitor_dirs.length > 0 }}",
                                                    "placeholder": "每一行一个目录，支持以下几种配置方式，转移方式支持 move、copy、link、softlink、rclone_copy、rclone_move：\n"
                                                    "监控目录:转移目的目录\n"
                                                    "监控目录:转移目的目录#转移方式\n"
                                                    "例如:\n/Downloads/电影/:/Library/电影/\n/Downloads/电视剧/:/Library/电视剧/",
                                                },
                                            }
                                        ],
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
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "排除关键词(正则, 区分大小写)",
                                            "rows": 1,
                                            "auto-grow": "{{ monitor_dirs.length > 0 }}",
                                            "placeholder": "每一行一个关键词",
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
                                "props": {"cols": 12},
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
                                                            "model": "move_failed_files",
                                                            "label": "移动失败文件",
                                                            "hint": "当转移失败时移动文件",
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
                                                            "model": "move_excluded_files",
                                                            "label": "移动匹配 屏蔽词/关键字 的文件",
                                                            "hint": "当命中过滤规则时移动文件",
                                                        },
                                                    }
                                                ],
                                            },
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
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "pathAfterMoveFailure",
                                            "label": "移动方式下，当整理失败或命中关键词后，将文件移动到此路径(会根据失败原因和原目录结构将文件移动到此处)",
                                            "rows": 1,
                                            "auto-grow": "{{ monitor_dirs.length > 0 }}",
                                            "placeholder": "只能有一个路径，留空或'转移方式'不是'移动'或关闭上面两个开关均不生效",
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
                                            "text": "1.入库消息延迟默认10s，如网络较慢可酌情调大，有助于发送统一入库消息。\n2.源目录与目的目录设置一致，则默认使用目录设置配置。否则可在源目录后拼接@覆盖方式（默认never覆盖方式）。\n3.开启软连接/Strm会在监控转移后联动【实时软连接】/【云盘Strm[助手]】插件生成软连接/Strm（只处理媒体文件，不处理刮削文件）。\n4.启用此插件后，可将`设定`--`存储&目录`--`目录`--`自动整理`改为`不整理`或`手动整理`\n\n此插件由thsrite的目录监控插件修改而得\n本意是为了做类似v1的定时整理，因我只用本地移动，原地整理，故也不知软/硬链、Strm之类的是否可用",
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
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "排除关键词推荐使用下面9行(一行一个):\n```\nSpecial Ending Movie\n\\[((TV|BD|\\bBlu-ray\\b)?\\s*CM\\s*\\d{2,3})\\]\n\\[Teaser.*?\\]\n\\[PV.*?\\]\n\\[NC[OPED]+.*?\\]\n\\[S\\d+\\s+Recap(\\s+\\d+)?\\]\n\\b(CDs|SPs|Scans|Bonus|映像特典|特典CD|/mv)\\b\n\\b(NC)?(Disc|SP|片头|OP|片尾|ED|PV|CM|MENU|EDPV|SongSpot|BDSpot)(\\d{0,2}|_ALL)\\b\n(?i)\\b(sample|preview|menu|special)\\b\n```\n排除bdmv再加入下面2行:\n```\n(?i)\\d+\\.(m2ts|mpls)$\n(?i)\\.bdmv$\n```\n",
                                            "style": {
                                                "white-space": "pre-line",
                                                "word-wrap": "break-word",
                                                "height": "auto",
                                                "max-height": "500px",
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
            "notify": False,
            "onlyonce": False,
            "history": False,
            "scrape": False,
            "category": False,
            "refresh": True,
            "softlink": False,
            "strm": False,
            "transfer_type": "move",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "interval": 10,
            "cron": "*/10 * * * *",
            "size": 0,
            "del_empty_dir": False,
            "downloaderSpeedLimit": 0,
            "downloaders": "不限速",
            "pathAfterMoveFailure": None,
            "move_failed_files": True,
            "move_excluded_files": True,
            "pre_cancel_speed_limit": False,
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

import re
import traceback
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app import schemas
from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import EventManager, eventmanager, Event
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils


class AutoShout(_PluginBase):
    # 插件名称
    plugin_name = "自动喊话"
    # 插件描述
    plugin_desc = "自动在指定站点发送特定消息。"
    # 插件图标
    plugin_icon = "reminders.png"
    # 插件版本
    plugin_version = "0.0.1"
    # 插件作者
    plugin_author = "viquu"
    # 作者主页
    author_url = "https://github.com/viquu"
    # 插件配置项ID前缀
    plugin_config_prefix = "autoshout_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites: SitesHelper = None
    siteoper: SiteOper = None
    sitechain: SiteChain = None
    # 事件管理器
    event: EventManager = None
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 加载的模块
    _site_schema: list = []

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _shout_sites: list = []
    _shout_text: str = ""

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.event = EventManager()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._shout_sites = config.get("shout_sites") or []
            self._shout_text = config.get("shout_text") or ""

            # 过滤掉已删除的站点
            all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in
                                                                                   self.__custom_sites()]
            self._shout_sites = [site_id for site_id in all_sites if site_id in self._shout_sites]
            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("自动喊话服务启动，立即运行一次")
                self._scheduler.add_job(func=self.shout, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="自动喊话")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        # 保存配置
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "shout_sites": self._shout_sites,
                "shout_text": self._shout_text,
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/shout",
            "event": EventType.PluginAction,
            "desc": "自动喊话",
            "category": "站点",
            "data": {
                "action": "shout"
            }
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            return [{
                "id": "AutoShout",
                "name": "自动喊话服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.shout,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": site.name, "value": site.id}
                         for site in self.siteoper.list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空则每天9点执行一次'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'shout_sites',
                                            'label': '喊话站点',
                                            'items': site_options
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'shout_text',
                                            'label': '喊话内容',
                                            'placeholder': '请输入要发送的消息内容'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "cron": "0 9 * * *",
            "onlyonce": False,
            "shout_sites": [],
            "shout_text": ""
        }

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    @eventmanager.register(EventType.PluginAction)
    def shout(self, event: Event = None):
        """
        自动喊话
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "shout":
                return
        
        if event:
            logger.info("收到命令，开始喊话 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始喊话 ...",
                              userid=event.event_data.get("user"))

        if self._shout_sites:
            self.__do(do_sites=self._shout_sites, event=event)

    def __do(self, do_sites: list, event: Event = None):
        """
        喊话逻辑
        """
        # 查询所有站点
        all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
        # 过滤掉没有选中的站点
        if do_sites:
            do_sites = [site for site in all_sites if site.get("id") in do_sites]
        else:
            logger.info("没有需要喊话的站点")
            return

        if not do_sites:
            logger.info(f"没有需要喊话的站点")
            return

        # 执行喊话
        logger.info(f"开始执行喊话任务 ...")
        with ThreadPool(min(len(do_sites), 1)) as p:
            status = p.map(self.shout_site, do_sites)

        if status:
            logger.info(f"喊话任务完成！")
            # 发送通知
            if self._notify:
                shout_message = "\n".join([f'【{s[0]}】{s[1]}' for s in status if s])
                self.post_message(title=f"【自动喊话】",
                                  mtype=NotificationType.SiteMessage,
                                  text=f"{shout_message}"
                                  )
            if event:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"喊话完成！", userid=event.event_data.get("user"))
        else:
            logger.error(f"喊话任务失败！")
            if event:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"喊话任务失败！", userid=event.event_data.get("user"))
        # 保存配置
        self.__update_config()

    def shout_site(self, site_info: CommentedMap) -> Tuple[str, str]:
        """
        在一个站点喊话
        """
        start_time = datetime.now()
        state, message = self.__shout_base(site_info, self._shout_text)
        # 统计
        seconds = (datetime.now() - start_time).seconds
        domain = StringUtils.get_url_domain(site_info.get('url'))
        if state:
            self.siteoper.success(domain=domain, seconds=seconds)
        else:
            self.siteoper.fail(domain)
        return site_info.get("name"), message

    @staticmethod
    def __shout_base(site_info: CommentedMap, shout_text: str) -> Tuple[bool, str]:
        """
        通用喊话处理
        :param site_info: 站点信息
        :param shout_text: 喊话内容
        :return: 喊话结果信息
        """
        if not site_info:
            return False, ""
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxies = settings.PROXY if site_info.get("proxy") else None
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法喊话")
            return False, ""
        
        # 喊话
        try:
            shout_url = urljoin(site_url, "shoutbox.php")
            params = {
                "shbox_text": shout_text,
                "shout": "我喊",
                "sent": "yes",
                "type": "shoutbox"
            }
            logger.info(f"开始在 {site} 喊话 ...")
            res = RequestUtils(cookies=site_cookie,
                               ua=ua,
                               proxies=proxies
                               ).get_res(url=shout_url, params=params)
            if res and res.status_code == 200:
                logger.info(f"{site} 喊话成功")
                return True, f"喊话成功"
            elif res is not None:
                logger.warn(f"{site} 喊话失败，状态码：{res.status_code}")
                return False, f"喊话失败，状态码：{res.status_code}！"
            else:
                logger.warn(f"{site} 喊话失败，无法打开网站")
                return False, f"喊话失败，无法打开网站！"
        except Exception as e:
            logger.warn("%s 喊话失败：%s" % (site, str(e)))
            traceback.print_exc()
            return False, f"喊话失败：{str(e)}！"

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

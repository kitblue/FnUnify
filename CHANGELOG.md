## 2026-10-16 聚合更新
- 应用总数: 320 -> 327
- 新增 44: bombjackgame, chipbeat, clamsentinel, clashlite, clawbot-gateway, com.dashboard.nasdash, com.gulugulupao.smartdisk, com.qweather.widget, cpu-tuner, dbx, dsh, dupclean, flymail, fn-wireguard, fnblackboard, fnmusic_ext_kugou, fnnas-notes, fnnas.hosts, fnos-terminal, fnwifi ...
- 移除 37: adguard, app-app, clouddrive, embyserver, fn-advancedsettings, fn-appdownload, fn-appsettings, fn-audioplayer, fn-bluetooth, fn-chromium-desktop, fn-execute, fn-fail2ban, fn-grafana-alloy, fn-guacamole, fn-influxdb, fn-installer, fn-kodi, fn-linux-station, fn-monitor, fn-open-vm-tools ...
- 补充遗漏源 19 个: yoqie/FnDepot, MisiteQ/FnDepot, lianhua/FnDepot, Digit-Space2021/FnDepot, shupian35/FnDepot, DPanel-dev/FnDepot, LiuLiuxc/FnDepot, 12hgl/FnDepot, tfgzs/FnDepot, xnkyn/FnDepot, bekafka/FnDepot, EzFavorites/FnDepot, cliii-one/FnDepot, x1027966382/FnDepot, zhixinfang/FnDepot, smartartian/FnDepot, ByronChen7/FnDepot, FNOSP/FnDepot, zhu-longjun/FnDepot（repo_list.txt 158 -> 177）
- 官方商店重复移除: 排除清单由 appname 精确匹配升级为 appname 精确/规范化/display_name 三通道 + 官方镜像源兜底，本次排除 202 个官方商店已收录应用
- 防清洗/防覆盖: 新增 keep_sources.txt（KITblue/fantune），不可达清洗豁免 + 同名覆盖强制优先 + 去重加权，保证 FanTune 不被清洗/顶替

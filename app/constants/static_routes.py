CONSTANT_ROUTES = [
    {
        "name": "403",
        "path": "/403",
        "component": "layout.blank$view.403",
        "meta": {
            "title": "403",
            "i18nKey": "route.403",
            "constant": True,
            "hideInMenu": True,
        },
    },
    {
        "name": "404",
        "path": "/404",
        "component": "layout.blank$view.404",
        "meta": {
            "title": "404",
            "i18nKey": "route.404",
            "constant": True,
            "hideInMenu": True,
        },
    },
    {
        "name": "500",
        "path": "/500",
        "component": "layout.blank$view.500",
        "meta": {
            "title": "500",
            "i18nKey": "route.500",
            "constant": True,
            "hideInMenu": True,
        },
    },
    {
        "name": "home",
        "path": "/home",
        "component": "layout.base$view.home",
        "meta": {
            "title": "home",
            "i18nKey": "route.home",
            "icon": "mdi:monitor-dashboard",
            "order": 1,
        },
    },
    {
        "name": "iframe-page",
        "path": "/iframe-page/:url",
        "component": "layout.base$view.iframe-page",
        "props": True,
        "meta": {
            "title": "iframe-page",
            "i18nKey": "route.iframe-page",
            "constant": True,
            "hideInMenu": True,
            "keepAlive": True,
        },
    },
    {
        "name": "login",
        "path": "/login/:module(pwd-login|code-login|register|reset-pwd|bind-wechat)?",
        "component": "layout.blank$view.login",
        "props": True,
        "meta": {
            "title": "login",
            "i18nKey": "route.login",
            "constant": True,
            "hideInMenu": True,
        },
    },
]

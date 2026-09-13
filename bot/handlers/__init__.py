"""Порядок роутеров значим.

`measure` стоит последним, потому что в нём висит `F.text` — обработчик голого
числа. Он не должен перехватывать ни команды, ни шаги мастера `/baby`, а
aiogram отдаёт апдейт первому роутеру, чей фильтр сошёлся.

`common` первым: в нём отказ для лички, и он обязан сработать раньше всего
остального, что могло бы попытаться достать из апдейта несуществующую семью.
"""

from aiogram import Router

from bot.handlers import baby, common, measure, track, views

routers: list[Router] = [
    common.router,
    views.router,
    track.router,
    baby.router,
    measure.router,
]

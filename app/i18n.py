"""
Простая локализация без внешних зависимостей.

Исходный язык интерфейса — русский: строки в коде и шаблонах написаны
по-русски и обёрнуты в _("..."). Для английского ведётся словарь переводов
ниже. Если перевода для строки нет — она просто выводится как есть (по-русски),
это безопасный fallback и одновременно подсказка, что строку надо перевести.
"""
from decimal import Decimal, InvalidOperation

from flask import session, request, g

SUPPORTED_LANGUAGES = {"ru": "Русский", "en": "English"}
DEFAULT_LANGUAGE = "ru"

# ru -> en
TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # nav / common
        "Гаражи": "Garages",
        "Люди": "People",
        "Лицевые счета": "Personal accounts",
        "Документы": "Documents",
        "Собрания": "Meetings",
        "Реквизиты": "Legal details",
        "Выйти": "Log out",
        "Сохранить": "Save",
        "Отмена": "Cancel",
        "Редактировать": "Edit",
        "Добавить": "Add",
        "Комментарий": "Comment",
        "Дата": "Date",
        "Сумма": "Amount",
        "ФИО": "Full name",
        "Email": "Email",
        "Телефоны": "Phones",

        # auth
        "Вход в систему": "Sign in",
        "Логин": "Username",
        "Пароль": "Password",
        "Войти": "Sign in",
        "Пожалуйста, войдите в систему.": "Please sign in.",
        "Недостаточно прав для этого действия.": "You don't have permission to do this.",
        "Неверный логин или пароль.": "Invalid username or password.",
        "Учётная запись отключена.": "This account is disabled.",

        # dashboard
        "Панель кооператива": "Cooperative dashboard",
        "Гаражей": "Garages",
        "Людей в базе": "People on file",
        "Последнее собрание": "Last meeting",
        "Последний годовой отчёт": "Last annual report",

        # cooperative
        "Реквизиты кооператива": "Cooperative legal details",
        "Расчётные счета": "Settlement accounts",
        "Добавить счёт": "Add account",
        "Новый расчётный счёт": "New settlement account",
        "Основной": "Primary",
        "основной": "primary",
        "Основной счёт (для платёжек ПД-4)": "Primary account (used for PD-4 payment slips)",
        "Расчётных счетов пока нет.": "No settlement accounts yet.",
        "Расчётный счёт добавлен.": "Settlement account added.",
        "Расчётный счёт обновлён.": "Settlement account updated.",
        "Расчётный счёт удалён.": "Settlement account deleted.",
        "Удалить расчётный счёт?": "Delete this settlement account?",
        "Баланс кооператива": "Cooperative balance",
        "сумма по всем лицевым счетам": "sum across all personal accounts",
        "Площади": "Areas",
        "Площадь кооператива": "Cooperative area",
        "Площадь кооператива, м²": "Cooperative area, m²",
        "Площадь под гаражами": "Area under garages",
        "Площадь под гаражами, м²": "Area under garages, m²",
        "Площадь общего пользования": "Common area",
        "Площадь общего пользования, м²": "Common area, m²",
        "Личный кабинет": "Personal cabinet",
        "Коэффициент начисления": "Charge multiplier",
        "Множитель для начислений на этот гараж — напр. 2 для двойного, 0.5 для маленького. По умолчанию 1.":
            "Multiplier used for charges on this garage — e.g. 2 for a double garage, 0.5 for a small one. Default is 1.",
        "Массовое начисление": "Mass charge",
        "Массовое начисление на лицевые счета": "Mass charge to member accounts",
        "По коэффициенту гаража": "By garage multiplier",
        "Указанная сумма начисляется на каждый гараж, умноженная на его коэффициент, и делится между собственниками по их доле. Например, взнос 1000 ₽: у гаража с коэффициентом 2 начислится 2000 ₽, с коэффициентом 0.5 — 500 ₽ (и далее по долям собственников).":
            "The given amount is charged to each garage, multiplied by its coefficient, then split between owners by their share. E.g. a 1000 ₽ fee: a garage with coefficient 2 gets charged 2000 ₽, one with 0.5 gets 500 ₽ (then split further by ownership share).",
        "сумма на гараж (до коэффициента), напр. 1000": "amount per garage (before multiplier), e.g. 1000",
        "От общей суммы, по площади": "From a total, by area",
        "Указанная общая сумма делится между гаражами пропорционально их площади, а внутри гаража — между собственниками по их доле.":
            "The given total is split between garages proportionally to their area, then within each garage between owners by their share.",
        "общая сумма, напр. 100000": "total amount, e.g. 100000",
        "Результат": "Result",
        "Итого начислено": "Total charged",
        "Пропущено (нет лицевого счёта)": "Skipped (no account)",
        "Начислено счетов: {n}.": "Charged accounts: {n}.",
        "Пропущено (нет лицевого счёта на этот вид взноса): {n}. Счета заводятся автоматически при добавлении собственника — проверьте вид взноса и код счёта.":
            "Skipped (no account for this fee type): {n}. Accounts are created automatically when an owner is added — check the fee type's account code.",
        "Полное наименование": "Full legal name",
        "Краткое наименование": "Short name",
        "ИНН": "Tax ID (INN)",
        "КПП": "Tax registration code (KPP)",
        "ОГРН": "State registration number (OGRN)",
        "Юридический адрес": "Registered address",
        "Почтовый адрес": "Mailing address",
        "Банк": "Bank",
        "БИК": "Bank ID (BIK)",
        "Расчётный счёт": "Checking account",
        "Корр. счёт": "Correspondent account",
        "Дата регистрации": "Registration date",
        "Реквизиты ещё не заполнены.": "Legal details not filled in yet.",
        "Реквизиты сохранены.": "Legal details saved.",

        # garages
        "№": "No.",
        "Площадь, м²": "Area, m²",
        "Земля приватизирована": "Land privatized",
        "Собственники": "Owners",
        "Пока нет ни одного гаража.": "No garages yet.",
        "Поиск по номеру гаража или собственнику...": "Search by garage number or owner...",
        "Ничего не найдено.": "Nothing found.",
        "+ Добавить гараж": "+ Add garage",
        "Новый гараж": "New garage",
        "Редактирование гаража": "Edit garage",
        "Номер гаража": "Garage number",
        "Земля под гаражом приватизирована": "Land under the garage is privatized",
        "Гараж №{n}": "Garage No. {n}",
        "Информация": "Information",
        "Электричество и счёт": "Electricity and account",
        "Лицевой счёт не создан.": "No personal account has been created.",
        "Гараж": "Garage",
        "гараж": "garage",
        "Площадь": "Area",
        "Кадастровый номер гаража": "Garage cadastral number",
        "Кадастровый номер участка": "Land plot cadastral number",
        "Площадь приватизированного участка": "Privatized plot area",
        "Площадь приватизированного участка, м²": "Privatized plot area, m²",
        "Используется для расчёта земельного налога — заполняется, если земля приватизирована.":
            "Used to calculate land tax — fill in if the land is privatized.",
        "Заполняется, если земля под гаражом приватизирована.": "Fill in if the land under the garage is privatized.",
        "Добавить собственника": "Add owner",
        "Новый член кооператива": "New cooperative member",
        "выберите члена кооператива": "select a cooperative member",
        "Фото не сохранено: поддерживаются только изображения (jpg, png, webp, gif).": "Photo not saved: only images are supported (jpg, png, webp, gif).",
        "да": "yes",
        "нет": "no",
        "Лицевой счёт": "Personal account",
        "Доля": "Share",
        "Собственники не указаны.": "No owners listed.",
        "Удалить собственника?": "Remove this owner?",
        "Лица для связи": "Contact persons",
        "Отношение": "Relation",
        "Контакты не указаны.": "No contacts listed.",
        "Удалить контакт?": "Remove this contact?",
        "сумма долей": "share total",
        "Гараж №{number} создан, лицевой счёт открыт.": "Garage No. {number} created, personal account opened.",
        "Гараж не найден.": "Garage not found.",
        "Изменения сохранены.": "Changes saved.",
        "Доля должна быть числом (например 0.5).": "Share must be a number (e.g. 0.5).",
        "Доля должна быть в диапазоне от 0 (не включая) до 1.": "Share must be between 0 (exclusive) and 1.",
        "Собственник добавлен/обновлён.": "Owner added/updated.",
        "Собственник удалён.": "Owner removed.",
        "Контактное лицо добавлено.": "Contact person added.",
        "Контактное лицо удалено.": "Contact person removed.",

        # persons
        "Человек": "Person",
        "Поиск по ФИО...": "Search by name...",
        "Никого не найдено.": "No one found.",
        "Новый человек": "New person",
        "Редактирование": "Edit",
        "Телефоны (через запятую)": "Phones (comma-separated)",
        "Telegram": "Telegram",
        "Адрес регистрации (прописки)": "Registered (permanent) address",
        "Адрес проживания": "Residential address",
        "Паспорт РФ": "Russian passport",
        "Серия": "Series",
        "Номер": "Number",
        "Код подразделения": "Issuing department code",
        "Дата выдачи": "Date issued",
        "Кем выдан": "Issued by",
        "Адрес регистрации": "Registered address",
        "Паспорт": "Passport",
        "Серия": "Series",
        "Номер": "Number",
        "Код подразделения": "Issuing department code",
        "Дата выдачи": "Date issued",
        "Кем выдан": "Issued by",
        "Адрес регистрации": "Registered address",
        "выдан": "issued",
        "Человек «{name}» добавлен.": "Person “{name}” added.",
        "Человек не найден.": "Person not found.",
        "Доступ в систему": "System access",
        "Учётные записи": "Accounts",
        "Печать ПД-4": "Print PD-4",
        "Печать платёжек ПД-4": "Print PD-4 slips",
        "Платёжки ПД-4": "PD-4 slips",
        "Все члены кооператива": "All cooperative members",
        "Если по счёту вида «пеня» тоже есть задолженность — его платёжка допечатается автоматически, даже если не отмечен ниже.":
            "If the matching penalty account also has a debt, its slip will be added automatically, even if not checked below.",
        "Показаны только ваши лицевые счета с задолженностью. Если по ним есть пеня — её платёжка допечатается автоматически.":
            "Only your accounts with a debt are shown. If there's a penalty on them, its slip will be added automatically.",
        "Долг": "Debt",
        "Печать выбранных": "Print selected",
        "Задолженностей нет — печатать нечего.": "No debts — nothing to print.",
        "Назад к выбору": "Back to selection",
        "Печать": "Print",
        "Получатель": "Payee",
        "Плательщик": "Payer",
        "Назначение платежа": "Payment purpose",
        "Выберите хотя бы один лицевой счёт.": "Select at least one account.",
        "Сначала заполните реквизиты кооператива.": "Fill in the cooperative's details first.",
        "По выбранным счетам нет задолженности — печатать нечего.": "The selected accounts have no debt — nothing to print.",
        "Ваша учётная запись не привязана к карточке члена кооператива — обратитесь в правление.":
            "Your account isn't linked to a member record — please contact the board.",
        "ИЗВЕЩЕНИЕ": "NOTICE",
        "КВИТАНЦИЯ": "RECEIPT",
        "Кассир": "Cashier",
        "К/сч": "Corr. acc.",
        "Р/сч": "Acc. No.",
        "Лицевой счет (код) плательщика": "Payer's personal account (code)",
        "ФИО плательщика": "Payer's full name",
        "Адрес плательщика": "Payer's address",
        "наименование банка получателя платежа": "name of the payee's bank",
        "руб": "rub",
        "Скачать PDF": "Download PDF",
        "Для скачивания PDF нужна библиотека weasyprint. Установите: pip install weasyprint":
            "The weasyprint library is required to download PDF. Install it: pip install weasyprint",
        "Бухгалтер": "Accountant",
        "Контрагенты": "Vendors",
        "Добавить контрагента": "Add vendor",
        "Новый контрагент": "New vendor",
        "Категория": "Category",
        "напр. уборка снега, электрика": "e.g. snow removal, electrical work",
        "Изменить": "Edit",
        "Контрагент добавлен.": "Vendor added.",
        "Данные контрагента обновлены.": "Vendor details updated.",
        "Контрагент удалён.": "Vendor deleted.",
        "Контрагентов пока нет.": "No vendors yet.",
        "Удалить контрагента?": "Delete this vendor?",
        "Нельзя удалить контрагента — по нему есть записи о расходах.": "Can't delete this vendor — there are expense records linked to it.",
        "Правление": "Board",
        "По": "Until",
        "действует сейчас": "in effect now",
        "общего пользования": "common area",
        "первая запись": "first entry",
        "нет тарифа на этот месяц": "no tariff for this month",
        "Тариф (справочно)": "Tariff (reference only)",
        "Сумма (справочно)": "Amount (reference only)",
        "Сумма рассчитывается автоматически: (новые показания − предыдущие) × тариф, действующий на месяц оплаты.":
            "The amount is calculated automatically: (new reading − previous) × the tariff in effect for the billing month.",
        "Нет тарифа, действующего на этот месяц — сначала добавьте тариф.": "No tariff is in effect for this month — add a tariff first.",
        "% банка за обслуживание": "Bank service fee, %",
        "Добавляется ко всем автоматическим начислениям (напр. 1.6 для Сбербанка).": "Added to all automatic charges (e.g. 1.6 for Sberbank).",
        "Стандартная площадь под гараж, м²": "Standard garage land plot, m²",
        "Для расчёта налога на землю под неприватизированным гаражом. По умолчанию 30 м².": "Used to calculate land tax for a non-privatized garage. Default is 30 m².",
        "Ставка налога, % от кадастровой стоимости": "Tax rate, % of cadastral value",
        "Земельный налог": "Land tax",
        "Земельный налог (автоматический расчёт)": "Land tax (automatic calculation)",
        "Считается по площадям кооператива и кадастровой стоимости за указанный год — под постройкой и общей территорией, с учётом приватизированных участков и % банка за обслуживание. Вид взноса определяется автоматически («Земельный налог»).":
            "Calculated from the cooperative's areas and the cadastral value for the given year — for the footprint and common area, accounting for privatized plots and the bank service fee. The fee type is set automatically (“Land tax”).",
        "кадастровая стоимость на указанный год, напр. 3892792.20": "cadastral value for the given year, e.g. 3892792.20",
        "Изменить можно в реквизитах кооператива.": "You can change this in the cooperative's details.",
        "В реквизитах кооператива не заполнены площади — расчёт невозможен.": "The cooperative's details are missing area values — calculation isn't possible.",
        "Не найден вид взноса «land_tax» — проверьте справочник видов взносов.": "The “land_tax” fee type wasn't found — check the fee types reference.",
        "Недостаточно данных для расчёта: заполните площади кооператива в его карточке и кадастровую стоимость на {year} год.":
            "Not enough data to calculate: fill in the cooperative's areas and the cadastral value for {year}.",
        "Светлая": "Light",
        "Тёмная": "Dark",
        "Учётных записей пока нет.": "No accounts yet.",
        "Роль": "Role",
        "Статус": "Status",
        "не привязана": "not linked",
        "выберите человека": "select a person",
        "Отвязать": "Unlink",
        "Привязать": "Link",
        "Отвязать эту учётную запись от человека?": "Unlink this account from the person?",
        "Здесь можно привязать существующую учётную запись к другому человеку или отвязать её (например, служебную запись «chairman»).":
            "Here you can link an existing account to a different person, or unlink it (e.g. the service “chairman” account).",
        "Выберите человека для привязки.": "Select a person to link.",
        "У этого человека уже есть другая учётная запись.": "This person already has a different account.",
        "Эта учётная запись уже привязана к человеку — сначала отвяжите.": "This account is already linked to a person — unlink it first.",
        "Учётная запись привязана.": "Account linked.",
        "Учётная запись отвязана от человека.": "Account unlinked from the person.",
        "Сменить логин": "Change username",
        "новый логин": "new username",
        "Логин не может быть пустым.": "Username cannot be empty.",
        "Логин обновлён.": "Username updated.",
        "Логин": "Username",
        "логин": "username",
        "пароль": "password",
        "новый пароль": "new password",
        "доступ активен": "access active",
        "доступ отключён": "access disabled",
        "Сбросить пароль": "Reset password",
        "Отключить доступ": "Disable access",
        "Включить доступ": "Enable access",
        "Создать доступ": "Create access",
        "У этого человека пока нет доступа к системе.": "This person doesn't have system access yet.",
        "У этого человека уже есть учётная запись.": "This person already has an account.",
        "Такой логин уже занят.": "That username is already taken.",
        "Учётная запись создана. Сообщите человеку логин и пароль.": "Account created. Share the username and password with them.",
        "Пароль обновлён.": "Password updated.",
        "Доступ включён.": "Access enabled.",
        "Доступ отключён.": "Access disabled.",
        "Дата вступления в кооператив": "Membership start date",
        "Дата выхода из кооператива": "Membership end date",
        "Членство": "Membership",
        "действующий член": "active member",
        "выбыл": "former member",

        # finance
        "Виды взносов": "Fee types",
        "Виды взносов и начислений": "Fee and charge types",
        "Код": "Code",
        "Название": "Name",
        "Пока не заведено ни одного вида взноса.": "No fee types yet.",
        "Новый вид взноса": "New fee type",
        "Баланс": "Balance",
        "Счёт": "Account",
        "Счетов пока нет.": "No accounts yet.",
        "Начисления": "Charges",
        "Начисления на гаражи": "Garage charges",
        "Ручное начисление любого вида взноса на гараж (например, разовая целевая доначисление).":
            "Manually charge any fee type to a garage (e.g. a one-off special assessment).",
        "Ручное начисление на гараж — на странице «Начисления на гаражи» в меню «Правление».":
            "To add a manual charge, use the \u201cGarage charges\u201d page under the \u201cBoard\u201d menu.",
        "Год": "Year",
        "Вид взноса": "Fee type",
        "Начислить": "Charge",
        "Начисление добавлено.": "Charge added.",
        "Номер счёта": "Account number",
        "Номер счёта обновлён.": "Account number updated.",
        "Такой номер счёта уже используется.": "That account number is already in use.",
        "Электросчёт": "Electricity account",
        "Электроэнергия": "Electricity supply",
        "Оплатить": "Pay",
        "Лицевой счёт на электричество для этого гаража не создан.": "No electricity account has been created for this garage.",
        "Нет задолженностей — печатать нечего.": "No outstanding balance — nothing to print.",
        "Поставщик электроэнергии": "Electricity supplier",
        "Указать поставщика": "Set supplier",
        "Поставщик пока не указан.": "Supplier not set yet.",
        "Данные поставщика сохранены.": "Supplier details saved.",
        "Телефон": "Phone",
        "Адрес": "Address",
        "Тариф": "Tariff",
        "Тариф, ₽/кВт·ч": "Tariff, ₽/kWh",
        "кВт·ч": "kWh",
        "действует с": "in effect from",
        "Действует с": "In effect from",
        "Тариф пока не задан.": "No tariff set yet.",
        "Добавить тариф": "Add tariff",
        "Новый тариф": "New tariff",
        "Тариф добавлен.": "Tariff added.",
        "Показания общего счётчика": "Master meter readings",
        "Внести показания": "Add reading",
        "Месяц": "Month",
        "Показания, кВт·ч": "Reading, kWh",
        "Документ": "Document",
        "Документ от энергосбыта": "Document from the utility company",
        "Показания общего счётчика внесены.": "Master meter reading added.",
        "Счёт за электроэнергию {month}.{year}": "Electricity bill {month}.{year}",
        "Сумма рассчитывается автоматически: (новые показания − предыдущие) × тариф, действующий на дату снятия показаний, и начисляется на счёт за электричество этого гаража.":
            "The amount is calculated automatically: (new reading − previous) × the tariff in effect on the reading date, and is charged to this garage's electricity account.",
        "Начислено по показаниям от {date}": "Charged based on the reading from {date}",
        "Показания внесены, начислено {amount} ₽.": "Reading recorded, charged {amount} ₽.",
        "Показания внесены. Сумма не рассчитана — задайте тариф на странице «Электроэнергия».":
            "Reading recorded. Amount not calculated — set a tariff on the “Electricity supply” page.",
        "Показания не могут быть меньше предыдущих ({baseline}). Если счётчик был заменён, сначала внесите новый прибор учёта.":
            "The reading cannot be lower than the previous one ({baseline}). If the meter was replaced, add the new meter first.",
        "Показания не могут быть меньше предыдущих ({baseline}).":
            "The reading cannot be lower than the previous one ({baseline}).",
        "Исправить последнее показание": "Correct the last reading",
        "Последнее показание исправлено.": "Last reading corrected.",
        "Некорректное значение показаний.": "Invalid reading value.",
        "Добавить счёт": "Add account",
        "Новый лицевой счёт": "New personal account",
        "Член кооператива": "Cooperative member",
        "оставьте пустым — сформируется автоматически": "leave blank to auto-generate",
        "Создать": "Create",
        "Счёт создан.": "Account created.",
        "Счёт удалён.": "Account deleted.",
        "Такой счёт уже существует.": "That account already exists.",
        "У этого вида взноса нет кода счёта — укажите номер счёта вручную.": "This fee type has no account code — enter the account number manually.",
        "Удалить счёт": "Delete account",
        "Удалить счёт вместе со всей историей начислений и платежей по нему?": "Delete this account along with all its charge and payment history?",
        "Формат лицевых счетов": "Account number format",
        "Формат номеров": "Number format",
        "При сохранении все уже существующие номера будут пересчитаны под новый формат, где это возможно без конфликтов.":
            "On save, all existing account numbers will be recalculated to the new format wherever possible without conflicts.",
        "Ширина номера гаража (цифр)": "Garage number width (digits)",
        "Ширина номера собственника (цифр)": "Owner number width (digits)",
        "Префикс счёта на электричество": "Electricity account prefix",
        "Префикс пени": "Penalty prefix",
        "Пример — электричество (гараж 95)": "Example — electricity (garage 95)",
        "Пример — взнос (гараж 95, вид «1»)": "Example — fee (garage 95, type “1”)",
        "Пример — пеня по нему": "Example — its penalty",
        "Сохранить и пересчитать существующие номера": "Save and recalculate existing numbers",
        "Формат обновлён, все существующие номера приведены к нему. Изменено: {changed}.":
            "Format updated, all existing numbers were converted. Changed: {changed}.",
        "Формат обновлён. Приведено к новому формату: {changed}. Не удалось из-за конфликта номеров: {failed} — поправьте их вручную на страницах счетов.":
            "Format updated. Converted: {changed}. Failed due to number conflicts: {failed} — fix those manually on the account pages.",
        "Лицевые счета на электричество": "Electricity accounts",
        "Счета членов кооператива": "Member accounts",
        "Лицевой счёт на электричество — общий на гараж, без разбивки между собственниками.":
            "The electricity account is shared per garage, not split between co-owners.",
        "Земельный налог, членские взносы и пени по ним — отдельный счёт на каждого собственника по каждому гаражу.":
            "Land tax, membership fees, and their penalties each get a separate account per owner per garage.",
        "Код счёта": "Account code",
        "Если задан «код счёта» — при добавлении собственника гаражу автоматически заводится его персональный лицевой счёт на этот вид взноса.":
            "If an “account code” is set, adding an owner to a garage automatically opens their personal account for this fee type.",
        "Код счёта (1 цифра)": "Account code (1 digit)",
        "напр. 1": "e.g. 1",
        "Пеня": "Penalty",
        "Это пеня по другому виду взноса": "This is a penalty for another fee type",
        "Оставьте пустым, если этому виду взноса не нужен отдельный лицевой счёт на человека.":
            "Leave blank if this fee type doesn't need its own per-member account.",
        "Управление кооперативом": "Cooperative governance",
        "Член правления": "Board member",
        "Может быть только у одного человека — при назначении снимается с предыдущего.":
            "Only one person can hold this — assigning it removes it from the previous chairman.",
        "Вид": "Type",
        "Начислений нет.": "No charges yet.",
        "Начислить (распределится по долям)": "Charge (will be split by ownership share)",
        "Платежи": "Payments",
        "От кого": "From",
        "Платежей нет.": "No payments yet.",
        "плательщик": "payer",
        "комментарий (необязательно)": "comment (optional)",
        "Зарегистрировать платёж": "Register payment",
        "Вид взноса добавлен.": "Fee type added.",
        "Лицевой счёт не найден.": "Personal account not found.",
        "Начисление добавлено и распределено между собственниками.": "Charge added and split between owners.",
        "Платёж зарегистрирован.": "Payment registered.",

        # documents
        "Добавить документ": "Add document",
        "Все": "All",
        "Тип": "Type",
        "Файл": "File",
        "Документов пока нет.": "No documents yet.",
        "скачать": "download",
        "Новый документ": "New document",
        "Тип документа": "Document type",
        "Название документа": "Document title",
        "Документ добавлен.": "Document added.",
        "Файл не найден.": "File not found.",

        # meetings
        "Общие собрания": "General meetings",
        "Добавить собрание": "Add meeting",
        "Повестка": "Agenda",
        "Годовой отчёт": "Annual report",
        "Секретарь": "Secretary",
        "Председатель": "Chairman",
        "Протокол": "Protocol",
        "Собраний пока нет.": "No meetings yet.",
        "файл": "file",
        "Новое собрание": "New meeting",
        "Это ежегодное отчётное собрание (отчёт председателя, смета)": "This is the annual reporting meeting (chairman's report, budget)",
        "Председатель на собрании": "Presiding chairman",
        "Протокол (документ)": "Protocol (document)",
        "не прикреплён": "not attached",
        "Сам файл протокола сначала загрузите в разделе «Документы».": "Upload the protocol file itself in the “Documents” section first.",
        "Собрание добавлено.": "Meeting added.",
        "Файл протокола": "Protocol file",
        "Протокол собрания от {date}": "Meeting protocol from {date}",

        # electricity
        "Электричество": "Electricity",
        "Счётчик": "Meter",
        "Текущий счётчик": "Current meter",
        "Номер счётчика": "Meter number",
        "Дата установки": "Installation date",
        "Дата опломбировки": "Sealing date",
        "Показания при установке": "Reading at installation",
        "Номер пломбы на счётчике": "Meter seal number",
        "Номер пломбы на вводном автомате": "Breaker seal number",
        "Счётчик не установлен.": "No meter registered yet.",
        "Заменить счётчик / опломбировку": "Replace meter / reseal",
        "Счётчик добавлен.": "Meter record added.",
        "Журнал показаний": "Reading log",
        "Журнал показаний и начисления": "Reading log and charges",
        "Показания, кВт·ч": "Reading, kWh",
        "Дата оплаты": "Payment date",
        "Записей пока нет.": "No records yet.",
        "аванс": "advance payment",
        "оплачено": "paid",
        "частично": "partial",
        "не оплачено": "unpaid",
        "Добавить прибор учета": "Add meter",
        "Заменить прибор учета": "Replace meter",
        "Показания": "Reading",
        "Дата снятия показаний": "Reading date",
        "Показаний пока нет.": "No readings yet.",
        "Внести показания": "Add reading",
        "Показания внесены.": "Reading added.",
        "Сначала добавьте счётчик.": "Add a meter first.",

        # cabinet (self-service)
        "Мой профиль": "My profile",
        "Мой гараж": "My garage",
        "Актуальные данные (можно менять самостоятельно)": "Up-to-date info (you can edit this yourself)",
        "Официальные данные (изменяются только правлением)": "Official records (edited by the board only)",
        "Ваша учётная запись пока не привязана к карточке члена кооператива. Обратитесь в правление.":
            "Your account isn't linked to a member record yet. Please contact the board.",
        "Если эти данные устарели или указаны неверно — сообщите председателю или члену правления.":
            "If this information is outdated or incorrect, please tell the chairman or a board member.",
        "Данные обновлены.": "Details updated.",
        "За вами не числится ни одного гаража.": "No garages are registered under your name.",
        "Открыть и обновить данные": "Open and update details",
        "На странице гаража вы можете обновить комментарий, фото и список лиц для связи. Площадь, номер и кадастровые данные меняет только правление.":
            "On the garage page you can update the comment, photos, and contact persons. Area, number, and cadastral data are changed by the board only.",
        "Комментарий обновлён.": "Comment updated.",

        # garage photos
        "Фото гаража": "Garage photos",
        "Фото пока нет.": "No photos yet.",
        "Подпись": "Caption",
        "Подпись (необязательно)": "Caption (optional)",
        "Загрузить фото": "Upload photo",
        "Удалить": "Delete",
        "Удалить фото?": "Delete this photo?",
        "Выберите файл фотографии.": "Please choose an image file.",
        "Поддерживаются только изображения (jpg, png, webp, gif).": "Only image files are supported (jpg, png, webp, gif).",
        "Фото добавлено.": "Photo added.",
        "Фото обновлено.": "Photo updated.",
        "Фото удалено.": "Photo deleted.",

        # misc placeholders
        "год": "year",
        "сумма": "amount",
        "доля, напр. 0.5": "share, e.g. 0.5",
        "код, напр. land_tax": "code, e.g. land_tax",
        "название, напр. Земельный налог": "name, e.g. Land tax",
        "напр. супруга": "e.g. spouse",

        # role labels
        "председатель": "chairman",
        "член правления": "board member",
        "член кооператива": "member",

        # person data revisions
        "Ожидают одобрения": "Pending approval",
        "Изменения отправлены на {date}.": "Changes submitted on {date}.",
        "Отправить на рассмотрение": "Submit for review",
        "Нет изменений для отправки.": "Nothing to submit — no changes detected.",
        "Изменения отправлены на рассмотрение председателю.": "Changes submitted to the chairman for review.",
        "История изменений персональных данных": "Personal data revision history",
        "отправил": "submitted by",
        "одобрил": "approved by",
        "отклонил": "rejected by",
        "ожидает": "pending",
        "одобрено": "approved",
        "отклонено": "rejected",
        "Посмотреть предлагаемые данные": "View proposed data",
        "Одобрить": "Approve",
        "Отклонить": "Reject",
        "Ожидают изменения": "Changes pending",
        "Все": "All",
        "Эта ревизия уже обработана.": "This revision has already been processed.",
        "Изменения для «{name}» одобрены и применены.": "Changes for “{name}” approved and applied.",
        "Изменения для «{name}» отклонены.": "Changes for “{name}” rejected.",
        "Ожидают рассмотрения": "Pending review",
    }
}

ROLE_LABELS = {
    "ru": {"chairman": "председатель", "board": "член правления", "accountant": "бухгалтер", "member": "член кооператива"},
    "en": {"chairman": "chairman", "board": "board member", "accountant": "accountant", "member": "member"},
}

DOC_TYPE_LABELS = {
    "ru": {
        "charter": "устав", "order": "приказ", "act": "акт",
        "letter": "письмо", "protocol": "протокол", "other": "прочее",
    },
    "en": {
        "charter": "charter", "order": "order", "act": "act",
        "letter": "letter", "protocol": "protocol", "other": "other",
    },
}


def get_locale() -> str:
    lang = session.get("lang")
    if lang in SUPPORTED_LANGUAGES:
        return lang
    best = request.accept_languages.best_match(list(SUPPORTED_LANGUAGES))
    return best or DEFAULT_LANGUAGE


def translate(text: str, **kwargs) -> str:
    locale = getattr(g, "locale", DEFAULT_LANGUAGE)
    if locale != DEFAULT_LANGUAGE:
        text = TRANSLATIONS.get(locale, {}).get(text, text)
    return text.format(**kwargs) if kwargs else text


def role_label(role_value: str) -> str:
    locale = getattr(g, "locale", DEFAULT_LANGUAGE)
    return ROLE_LABELS.get(locale, ROLE_LABELS["ru"]).get(role_value, role_value)


def doc_type_label(doc_type_value: str) -> str:
    locale = getattr(g, "locale", DEFAULT_LANGUAGE)
    return DOC_TYPE_LABELS.get(locale, DOC_TYPE_LABELS["ru"]).get(doc_type_value, doc_type_value)


DATE_FORMATS = {"ru": "%d.%m.%Y", "en": "%Y-%m-%d"}


def format_date(value) -> str:
    """Форматирует date/datetime по текущей локали (используется везде вместо голого str(date))."""
    if value is None:
        return "—"
    locale = getattr(g, "locale", DEFAULT_LANGUAGE)
    return value.strftime(DATE_FORMATS.get(locale, DATE_FORMATS["ru"]))


def fmt2(value) -> str:
    """
    Форматирует число ровно с 2 знаками после запятой (рубли, м², доля, кВт·ч
    и т.п. — везде в интерфейсе). Разделитель дробной части — по локали
    (запятая для ru, точка для en). None/пусто -> "—".
    """
    if value is None or value == "":
        return "—"
    try:
        quantized = Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation:
        return str(value)
    text = f"{quantized:.2f}"
    locale = getattr(g, "locale", DEFAULT_LANGUAGE)
    if locale == "ru":
        text = text.replace(".", ",")
    return text


def init_app(app):
    @app.before_request
    def _set_locale():
        g.locale = get_locale()

    app.jinja_env.globals["_"] = translate
    app.jinja_env.globals["role_label"] = role_label
    app.jinja_env.globals["doc_type_label"] = doc_type_label
    app.jinja_env.globals["fmt_date"] = format_date
    app.jinja_env.globals["fmt2"] = fmt2
    app.jinja_env.globals["SUPPORTED_LANGUAGES"] = SUPPORTED_LANGUAGES

    @app.context_processor
    def _inject_locale():
        return {"current_locale": getattr(g, "locale", DEFAULT_LANGUAGE)}

    @app.route("/set-language/<lang>")
    def set_language(lang):
        from flask import redirect, request as req
        if lang in SUPPORTED_LANGUAGES:
            session["lang"] = lang
        return redirect(req.referrer or "/")

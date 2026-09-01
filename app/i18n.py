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
# Флаг для пиктограммы в переключателе языка (эмодзи — без внешних иконок/шрифтов)
LANGUAGE_FLAGS = {"ru": "🇷🇺", "en": "🇬🇧"}

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
        "О кооперативе": "About the cooperative",
        "Кооператив": "Cooperative",
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
        "Переключить тему": "Toggle theme",

        # news
        "Новости кооператива": "Cooperative news",
        "Новостей пока нет.": "No news yet.",
        "обновлено": "updated",
        "Новости": "News",
        "Добавить новость": "Add news item",
        "Новости показываются на главной странице (до входа в систему) — последние {n} записей.":
            "News is shown on the front page (before sign-in) — the latest {n} entries.",
        "Заголовок": "Title",
        "Автор": "Author",
        "изм.": "upd.",
        "Удалить новость?": "Delete this news item?",
        "Новость добавлена.": "News item added.",
        "Новость обновлена.": "News item updated.",
        "Новость удалена.": "News item deleted.",
        "Новая новость": "New news item",
        "Редактирование новости": "Edit news item",
        "Текст": "Text",
        "Читать дальше": "Read more",
        "Ко всем новостям": "Back to news",
        "Файлы": "Files",

        # wiki
        "Вики": "Wiki",
        "Добавить страницу": "Add page",
        "Справочные заметки кооператива: параметры подключений, схемы, контакты подрядчиков и аварийных служб и т.п.":
            "Reference notes for the cooperative: connection settings, network diagrams, contractor and emergency service contacts, etc.",
        "Все категории": "All categories",
        "например: видеонаблюдение, сеть, контакты": "e.g. CCTV, network, contacts",
        "Необязательно — используется для фильтра в списке страниц.": "Optional — used as a filter in the page list.",
        "Обновлено": "Updated",
        "внутренняя": "internal",
        "общедоступная": "public",
        "Страниц пока нет.": "No pages yet.",
        "Удалить страницу вики?": "Delete this wiki page?",
        "Новая страница вики": "New wiki page",
        "Редактирование страницы вики": "Edit wiki page",
        "Внутренняя страница (видна только правлению)": "Internal page (visible to the board only)",
        "Страница вики добавлена.": "Wiki page added.",
        "Страница вики обновлена.": "Wiki page updated.",
        "Страница вики удалена.": "Wiki page deleted.",
        "Ко всем страницам": "Back to all pages",
        "Родительская страница": "Parent page",
        "— нет (корневой раздел) —": "— none (root section) —",
        "Необязательно — без родителя страница станет корневым разделом дерева.":
            "Optional — without a parent, the page becomes a root section of the tree.",
        "Добавить подраздел": "Add subsection",
        "Сначала удалите или перенесите подразделы.": "Delete or move the subsections first.",
        "Нельзя удалить раздел, в котором есть подразделы/страницы — сначала удалите или перенесите их.":
            "Can't delete a section that still has subsections/pages — delete or move them first.",
        "Нельзя сделать родителем саму страницу или её же подраздел.": "The page itself or its own subsection can't be set as its parent.",

        "Форматирование": "Formatting",
        "Вставить картинку": "Insert image",
        "Кнопка с камерой загружает картинку и сразу вставляет её в текст в месте курсора.":
            "The camera button uploads an image and inserts it into the text at the cursor position right away.",
        "Файл не выбран.": "No file selected.",
        "Недопустимый формат файла. Разрешены: jpg, png, webp, gif.": "Unsupported file format. Allowed: jpg, png, webp, gif.",
        "Не удалось загрузить файл.": "Failed to upload the file.",
        "Жирный": "Bold",
        "Курсив": "Italic",
        "Подзаголовок": "Heading",
        "Список": "List",
        "Ссылка": "Link",
        "текст ссылки": "link text",
        "Адрес ссылки:": "Link URL:",
        "Поддерживается упрощённая разметка: **жирный**, *курсив*, [текст](ссылка), списки (пустая строка, затем строки с «- »), заголовок (### ). Между абзацами оставляйте пустую строку.":
            "Simplified formatting is supported: **bold**, *italic*, [text](link), lists (blank line, then lines starting with \"- \"), heading (### ). Leave a blank line between paragraphs.",
        "Прикреплённые файлы": "Attached files",
        "Отметьте, чтобы удалить при сохранении.": "Check to remove on save.",
        "Добавить фото или файлы": "Add photos or files",
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
        "Информация о кооперативе": "Ibformation about cooperative",
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
        "сумма по расчётным счетам в банках": "sum across the cooperative's bank accounts",
        "Площади": "Areas",
        "Площадь кооператива": "Cooperative area",
        "Площадь кооператива, м²": "Cooperative area, m²",
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
        "Предполагаемый счёт": "Suggested account",
        "л/с": "acct.",
        "Доля": "Share",
        "Собственники не указаны.": "No owners listed.",
        "История собственников": "Ownership history",
        "например: перераспределение долей между супругами": "e.g. share redistribution between spouses",
        "Удалить собственника: {name}": "Remove owner: {name}",
        "Вместе с собственником будут удалены его лицевые счета по этому гаражу (взносы, налог, пеня). Причина сохранится в истории собственников.":
            "The owner's personal accounts for this garage (dues, tax, penalty) will be removed along with them. The reason will be kept in the ownership history.",
        "Причина": "Reason",
        "например: продал, умер, унаследовал": "e.g. sold, deceased, inherited",
        "комментарий, например: купил, унаследовал": "comment, e.g. bought, inherited",
        "Событие": "Event",
        "добавлен": "added",
        "доля изменена": "share changed",
        "Выбыл": "Removed",
        "Причина/комментарий": "Reason/comment",
        "Кто внёс": "Recorded by",
        "История изменений собственников пока пуста.": "No ownership change history yet.",
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
        "Выписка по счетам": "Account statement",
        "Выписка по лицевым счетам": "Statement of personal accounts",
        "по состоянию на": "as of",
        "У этого человека пока нет лицевых счетов.": "This person doesn't have any personal accounts yet.",
        "Начислено": "Charged",
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
        "Нельзя удалить контрагента — по нему есть записи о расходах или платежах.": "Can't delete this vendor — there are expense or payment records linked to it.",
        "Правление": "Board",
        "Управление": "Management",
        "Реестры": "Registries",
        "Финансы": "Finance",
        "Контент": "Content",
        "Администрирование": "Administration",
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
        "Баланс, ₽": "Balance, ₽",
        "Баланс на дату": "Balance as of",
        "сумма по лицевым счетам членов": "sum across members' personal accounts",
        "Рядовой член кооператива": "Regular cooperative member",
        "состав правления, председатель и бухгалтер назначаются на странице": "the board's composition, chairman, and accountant are set on the",
        "(на основании протокола общего собрания).": "(based on the general meeting protocol).",
        "на": "as of",
        "Фактический баланс (счета + касса), вносится вручную. Не путать с внутренним учётным балансом на дашборде — тот считается автоматически как сумма лицевых счетов.":
            "Actual balance (bank accounts + cash on hand), entered manually. Not to be confused with the internal accounting balance on the dashboard, which is calculated automatically as the sum of personal accounts.",
        "Вносится вручную, нет интеграции с банком.": "Entered manually, no bank integration.",
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
        "Текущий пароль указан неверно.": "The current password is incorrect.",
        "Новый пароль слишком короткий (минимум 4 символа).": "The new password is too short (minimum 4 characters).",
        "Новый пароль и подтверждение не совпадают.": "The new password and confirmation don't match.",
        "Пароль изменён.": "Password changed.",
        "Изменить пароль": "Change password",
        "Текущий пароль": "Current password",
        "Новый пароль": "New password",
        "Повторите новый пароль": "Repeat new password",
        "Сменить пароль": "Change password",
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
        "Ручное начисление любого вида взноса на один, несколько или сразу все гаражи (например, разовая целевая доначисление).":
            "Manually charge any fee type to one, several, or all garages at once (e.g. a one-off special assessment).",
        "«Электричество» начисляется на гараж целиком (общий лицевой счёт на электричество). Все остальные виды взносов — на лицевые счета собственников гаража, сумма делится между ними пропорционально их долям владения; если у собственника ещё нет лицевого счёта на этот вид взноса, он будет пропущен.":
            "\u201cElectricity\u201d is charged to the garage as a whole (shared electricity account). All other fee types are charged to the garage owners' personal accounts, split proportionally to their ownership share; an owner without an account for that fee type is skipped.",
        "Поиск по номеру или собственнику…": "Search by number or owner…",
        "Выбрать все": "Select all",
        "Снять все": "Deselect all",
        "гаражей выбрано": "garages selected",
        "Ручное начисление любого вида взноса на гараж (например, разовая целевая доначисление).":
            "Manually charge any fee type to a garage (e.g. a one-off special assessment).",
        "Ручное начисление на гараж — на странице «Начисления на гаражи» в меню «Правление».":
            "To add a manual charge, use the \u201cGarage charges\u201d page under the \u201cBoard\u201d menu.",
        "Год": "Year",
        "Вид взноса": "Fee type",
        "Начислить": "Charge",
        "Начисление добавлено.": "Charge added.",
        "Начисление добавлено на {count} гаражей.": "Charge added to {count} garages.",
        "Начислений добавлено: {count}.": "Charges added: {count}.",
        "Пропущено (нет лицевого счёта на этот вид взноса у собственника): {n}. Счета заводятся автоматически при добавлении собственника гаража.":
            "Skipped (owner has no account for this fee type): {n}. Accounts are created automatically when a garage owner is added.",
        "Выберите хотя бы один гараж.": "Select at least one garage.",
        "Номер счёта": "Account number",
        "Номер счёта обновлён.": "Account number updated.",
        "По умолчанию": "Default",
        "У вида взноса «{name}» нет кода для формулы номера — задайте номер вручную.":
            "The fee type «{name}» has no code for the number formula — set the account number manually.",
        "Не удалось вычислить номер по умолчанию.": "Couldn't compute the default account number.",
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
        "счёт": "charge",
        "платёж": "payment",
        "Зарегистрировать": "Register",
        "Проверьте год и сумму начисления.": "Check the charge year and amount.",
        "Сумма начисления должна быть больше нуля.": "The charge amount must be greater than zero.",
        "Проверьте дату и сумму платежа.": "Check the payment date and amount.",
        "Сумма платежа должна быть больше нуля.": "The payment amount must be greater than zero.",

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
        "Внутренний документ (виден только правлению)": "Internal document (visible to the board only)",
        "Доступ": "Access",
        "внутренний": "internal",
        "общедоступный": "public",
        "Документ не найден.": "Document not found.",
        "Документ обновлён.": "Document updated.",
        "Документ удалён.": "Document deleted.",
        "Редактирование документа": "Edit document",
        "Скачать": "Download",
        "Текущий файл: ": "Current file: ",
        "Удалить документ?": "Delete this document?",
        "Поиск по названию, номеру или типу...": "Search by title, number or type...",

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
        "Гараж (все собственники)": "Garage (all owners)",
        "Лицевых счетов пока нет.": "No personal accounts yet.",
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
        "Показания внесены.": "Reading added.",
        "Сначала добавьте счётчик.": "Add a meter first.",

        # electricity phase monitoring (eWeLink POWCT)
        "Мониторинг фаз": "Phase monitoring",
        "Мониторинг электроэнергии по фазам": "Electricity monitoring by phase",
        "Устройства": "Devices",
        "Подключение к eWeLink": "eWeLink connection",
        "Подключение к eWeLink ещё не настроено.": "The eWeLink connection isn't configured yet.",
        "Заполните данные в разделе «Подключение к eWeLink» выше.": "Fill in the details in the “eWeLink connection” section above.",
        "Обратитесь к председателю.": "Please contact the chairman.",
        "Последняя попытка опроса устройств завершилась ошибкой:": "The last device poll failed with an error:",
        "Последний опрос:": "Last poll:",
        "Устройства ещё не привязаны к фазам.": "No devices are linked to phases yet.",
        "онлайн": "online",
        "офлайн": "offline",
        "нет данных": "no data",
        "Вт": "W",
        "кВт": "kW",
        "В": "V",
        "А": "A",
        "Суммарная мощность": "Total power",
        "Недостаточно данных.": "Not enough data.",
        "История": "History",
        "3 часа": "3 hours",
        "24 часа": "24 hours",
        "7 дней": "7 days",
        "30 дней": "30 days",
        "Сбросить масштаб": "Reset zoom",
        "Прокрутка колеса мыши — масштаб, перетаскивание — сдвиг по времени.": "Mouse wheel — zoom, drag — pan along the time axis.",
        "Нет данных за выбранный период.": "No data for the selected period.",
        "Не удалось загрузить историю — проверьте соединение и попробуйте ещё раз.": "Couldn't load the history — check your connection and try again.",
        "Устройства по фазам": "Devices by phase",
        "Device ID устройства смотрите в приложении eWeLink: Настройки устройства → Информация об устройстве → Device ID.":
            "Find the device ID in the eWeLink app: Device settings → Device info → Device ID.",
        "Вход выполняется по email и паролю от аккаунта eWeLink (не официальный OAuth2 Open API). App ID/App Secret — идентификатор клиентского приложения, которым вошли; для собственного тестового приложения их можно получить на dev.ewelink.cc.":
            "Login uses the eWeLink account's email and password (not the official OAuth2 Open API). App ID/App Secret identify the client application used to log in; you can get your own for testing at dev.ewelink.cc.",
        "Проверить подключение": "Test connection",
        "Настройки подключения к eWeLink сохранены.": "eWeLink connection settings saved.",
        "Устройства сохранены.": "Devices saved.",
        "Сначала заполните все поля подключения к eWeLink.": "Fill in all eWeLink connection fields first.",
        "Подключение к eWeLink работает.": "The eWeLink connection works.",
        "Ошибка подключения к eWeLink: {error}": "eWeLink connection error: {error}",
        "Фаза A": "Phase A",
        "Фаза B": "Phase B",
        "Фаза C": "Phase C",

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
        "доля, по умолчанию 1 (100%)": "share, defaults to 1 (100%)",
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
        "Архив": "Archive",
        "В архиве": "Archived",
        "отметил": "marked by",
        "Вернуть из архива": "Restore from archive",
        "В архив": "Archive",
        "Отправить в архив: {name}": "Archive: {name}",
        "например: умер, продал гараж и выбыл": "e.g. deceased, sold the garage and left",
        "Отправить в архив": "Send to archive",
        "Этот человек уже в архиве.": "This person is already archived.",
        "Человек отправлен в архив.": "Person archived.",
        "Человек возвращён из архива.": "Person restored from archive.",
        "Скрывается из общего реестра людей, карточка и выписка остаются доступны по прямой ссылке. Если этот человек — совладелец гаража (не единственный), он автоматически убирается из собственников с этой же причиной, доли оставшихся пересчитываются, остаток его лицевых счетов по этому гаражу переходит к ним пропорционально новым долям. Если он единственный собственник — гараж и его счета не трогаются, остаются как есть до появления нового собственника.":
            "Hidden from the general people list; the profile and statement stay reachable by direct link. If this person co-owns a garage (not sole owner), they're automatically removed from ownership with the same reason, the remaining owners' shares are recalculated, and the balance of their personal accounts for that garage passes to the remaining owners in proportion to the new shares. If they're the sole owner, the garage and its accounts are left untouched until a new owner appears.",
        "Эта ревизия уже обработана.": "This revision has already been processed.",
        "Изменения для «{name}» одобрены и применены.": "Changes for “{name}” approved and applied.",
        "Изменения для «{name}» отклонены.": "Changes for “{name}” rejected.",
        "Ожидают рассмотрения": "Pending review",

        # --- Модуль расчётов с контрагентами (Expense/CounterpartyPayment/ReconciliationAct) ---
        "Открыть": "Open",
        "Расходы (начислено)": "Expenses (accrued)",
        "Добавить расход": "Add expense",
        "Расходов пока нет.": "No expenses yet.",
        "Оплачено": "Paid",
        "из": "of",
        "Счёт/акт": "Invoice/act",
        "Платежи (оплачено)": "Payments (paid)",
        "Добавить платёж": "Add payment",
        "Счёт списания": "Debited from",
        "Платежей пока нет.": "No payments yet.",
        "Платёжный документ": "Payment document",
        "Акты сверки": "Reconciliation acts",
        "Акт": "Act",
        "Добавить акт сверки": "Add reconciliation act",
        "Период": "Period",
        "Наш баланс": "Our balance",
        "Баланс по данным контрагента": "Vendor's balance",
        "расхождение": "discrepancy",
        "Актов сверки пока нет.": "No reconciliation acts yet.",
        "Новый расход": "New expense",
        "Сумма, ₽": "Amount, ₽",
        "напр. видеонаблюдение, электрика, уборка территории": "e.g. video surveillance, electrical work, area cleaning",
        "Описание": "Description",
        "Счёт / акт от контрагента": "Invoice/act from vendor",
        "Новый платёж": "New payment",
        "— наличными / не указан —": "— cash / not specified —",
        "Если выбран счёт, при сохранении сумма спишется с его баланса.": "If an account is selected, the amount is debited from its balance on save.",
        "Новый акт сверки": "New reconciliation act",
        "Период с": "Period from",
        "по": "to",
        "Наш баланс, ₽": "Our balance, ₽",
        "По умолчанию — текущий расчётный баланс.": "Defaults to the current calculated balance.",
        "Баланс по данным контрагента, ₽": "Vendor's balance, ₽",
        "Подписанный акт": "Signed act",
        "Баланс расчётов": "Settlement balance",
        "кооператив должен контрагенту": "cooperative owes the vendor",
        "переплата в пользу кооператива": "overpayment in the cooperative's favor",
        "расчёты закрыты": "settled",
        "← Все контрагенты": "← All vendors",
        "Расход добавлен.": "Expense added.",
        "Счёт от {name}": "Invoice from {name}",
        "Платёж добавлен.": "Payment added.",
        "Платёжный документ — {name}": "Payment document — {name}",
        "Акт сверки добавлен.": "Reconciliation act added.",
        "Акт сверки — {name}": "Reconciliation act — {name}",

        # --- /power/readings/new: оплата поставщика прямо из формы показаний ---
        "Оплата": "Payment",
        "Оплатить со счёта": "Pay from account",
        "— не оплачивать сейчас, оплатить позже —": "— don't pay now, pay later —",
        "Если выбрать счёт, при сохранении сразу спишется сумма и расход перед поставщиком будет отмечен оплаченным. Если не выбирать — расход останется неоплаченным, оплатить можно будет позже в карточке контрагента.":
            "If you select an account, the amount is debited immediately and the expense is marked as paid. If left unselected, the expense stays unpaid and can be paid later from the vendor's page.",
        "Сумма рассчитывается автоматически: (новые показания − предыдущие) × тариф, действующий на месяц оплаты. При сохранении автоматически заводится расход перед поставщиком (раздел «Контрагенты»).":
            "The amount is calculated automatically: (new reading − previous) × the rate in effect for the billing month. An expense against the supplier (see “Vendors”) is created automatically on save.",
        "Показания внесены, но расход перед поставщиком не создан — сначала укажите поставщика электроэнергии.":
            "Reading saved, but no expense was created against the supplier — please set the electricity supplier first.",
        "Электроэнергия за {month}.{year} (общий счётчик)": "Electricity for {month}.{year} (master meter)",
        "Оплата за электроэнергию {month}.{year}": "Electricity payment for {month}.{year}",
        "Запись за {year}-{month:02d} уже существует.": "A record for {year}-{month:02d} already exists.",
        "Действия": "Actions",
        "Удалить показание?": "Delete reading?",
        "Показание удалено.": "Reading deleted.",
        "Нельзя удалить показание — по связанному расходу перед поставщиком уже есть оплата. Сначала разберитесь с платежом в карточке контрагента.":
            "Cannot delete the reading — the related expense against the supplier already has a payment. Resolve the payment on the vendor's page first.",
        "Можно удалить только самое последнее показание — иначе исказятся суммы уже сохранённых последующих записей.":
            "Only the most recent reading can be deleted — otherwise the amounts already stored for later records would become incorrect.",

        # --- Начальный баланс контрагента, правка/сторно последнего платежа ---
        "Начальный баланс": "Opening balance",
        "Начальный баланс, ₽": "Opening balance, ₽",
        "Начальный баланс на дату": "Opening balance as of",
        "Если расчёты велись ещё до учёта в системе — остаток на момент начала (тот же знак: отрицательное — кооператив уже был должен).":
            "If dealings with this vendor predate tracking in the system — the balance at the starting point (same sign: negative means the cooperative already owed money).",
        "Изменить платёж": "Edit payment",
        "Только для последнего платежа — если ошиблись в сумме или дате при вводе. Если платёж реально ушёл в банк и потом вернулся — используйте «Сторно».":
            "Only for the most recent payment — if you made a mistake in the amount or date. If the payment actually went to the bank and was later returned, use “Reversal” instead.",
        "Платёж изменён.": "Payment updated.",
        "Редактировать можно только последний платёж контрагенту.": "Only the most recent payment to a vendor can be edited.",
        "Это отменяющая проводка (сторно) — её нельзя редактировать.": "This is a reversal entry — it cannot be edited.",
        "Этот платёж уже сторнирован — редактировать его нельзя.": "This payment has already been reversed — it cannot be edited.",
        "Сторно": "Reversal",
        "сторно": "reversal",
        "Сторнировать": "Reverse",
        "Дата возврата": "Return date",
        "напр. ошибка в реквизитах, банк вернул платёж": "e.g. wrong bank details, payment returned by the bank",
        "Сторно платежа от {date}": "Reversal of the payment from {date}",
        "сторно платежа от {date}": "reversal of the payment from {date}",
        "отменён проводкой от {date}": "reversed by the entry dated {date}",
        "Для платежа, который реально ушёл в банк с ошибкой в реквизитах/организации, а потом вернулся (банком или контрагентом). Сам платёж останется в истории как есть, рядом появится отменяющая проводка на {amount} ₽.":
            "For a payment that actually went to the bank with wrong details/organization and was later returned (by the bank or the vendor). The original payment stays in the history as-is; a reversal entry for {amount} ₽ will appear alongside it.",
        "Это уже отменяющая проводка (сторно) — сторнировать сторно нельзя.": "This is already a reversal entry — a reversal cannot itself be reversed.",
        "Этот платёж уже сторнирован.": "This payment has already been reversed.",
        "Отменяющая проводка добавлена.": "Reversal entry added.",

        # --- Поставщик электроэнергии выбирается из справочника контрагентов ---
        "Контрагент": "Vendor",
        "Нет нужного контрагента в списке?": "Don't see the vendor you need?",
        "Добавьте его в разделе «Контрагенты»": "Add it in the “Vendors” section",
        "— не выбран —": "— not selected —",
        "вся история": "full history",
        "Реквизиты, расходы и платежи по поставщику — в карточке контрагента (ссылка выше). Баланс здесь — это баланс расчётов с ним же (раздел «Контрагенты»), отдельного баланса у поставщика нет.":
            "The supplier's details, expenses, and payments are on its vendor page (link above). The balance shown here is that same settlement balance (see “Vendors”) — there is no separate “supplier balance”.",

        # --- Правление: созывы, ревизионная комиссия ---
        "Правление кооператива": "Cooperative board",
        "Журнал аудита": "Audit log",
        "Все начисления, платежи, изменения ролей и доступа — кто и когда сделал. Записи не редактируются.":
            "All charges, payments, role and access changes — who did what and when. Entries cannot be edited.",
        "Когда": "When",
        "Кто": "Who",
        "Что": "What",
        "аноним/неизвестно": "anonymous/unknown",
        "Далее": "Next",
        "Созывы правления": "Board terms",
        "Новый созыв": "New term",
        "Каждый новый созыв избирается общим собранием (протоколом) и автоматически закрывает предыдущий. Состав вносится на странице созыва; чтобы состав реально повлиял на права входа в систему, на странице созыва нужно нажать «Применить состав».":
            "Each new term is elected by the general meeting (a protocol) and automatically closes the previous one. Membership is entered on the term's page; for it to actually affect system access, click “Apply composition” on that page.",
        "Состав": "Members",
        "по настоящее время": "to present",
        "текущий": "current",
        "чел.": "people",
        "Созывов пока нет.": "No terms yet.",
        "Ревизионная комиссия": "Revision commission",
        "Новый состав": "New composition",
        "Председатель комиссии": "Commission chair",
        "текущая": "current",
        "Составов ревизионной комиссии пока нет.": "No revision commission compositions yet.",
        "Новый созыв правления": "New board term",
        "Дата начала полномочий": "Start date",
        "Протокол общего собрания": "General meeting protocol",
        "Нет нужного протокола?": "Don't see the protocol you need?",
        "Добавьте собрание": "Add a meeting",
        "Действующий созыв (если есть) будет автоматически закрыт этой датой.": "The current term (if any) will be automatically closed as of this date.",
        "Новый состав ревизионной комиссии": "New revision commission composition",
        "Действующий состав (если есть) будет автоматически закрыт этой датой.": "The current composition (if any) will be automatically closed as of this date.",
        "← Все созывы": "← All terms",
        "Избран протоколом от": "Elected by the protocol dated",
        "скачать протокол": "download protocol",
        "Добавить в состав": "Add to roster",
        "Применить состав к правам доступа": "Apply composition to system access",
        "Применить этот состав к правам доступа? У людей, не входящих в список, права правления/председателя/бухгалтера будут сняты; у входящих — выставлены по отмеченным флагам.":
            "Apply this composition to system access? People not on the list will have their board/chairman/accountant access revoked; people on the list will get it set according to the flags marked.",
        "Закрыть созыв": "Close term",
        "Закрыть этот созыв досрочно?": "Close this term early?",
        "Добавление/удаление людей само по себе прав входа не меняет — это только список. Права входа (роль в системе) обновляются нажатием «Применить состав к правам доступа».":
            "Adding or removing people by itself does not change system access — it's just a list. Access (the system role) is updated by clicking “Apply composition to system access”.",
        "Роль (доп.)": "Role (extra)",
        "бухгалтер": "accountant",
        "Убрать": "Remove",
        "Убрать из состава?": "Remove from the roster?",
        "Добавить в состав созыва": "Add to the term's roster",
        "Председатель (снимет флаг с текущего, если был)": "Chairman (will unset the flag from the current one, if any)",
        "Роль (доп., необязательно)": "Role (extra, optional)",
        "напр. секретарь, казначей": "e.g. secretary, treasurer",
        "Состав пока не внесён.": "No members entered yet.",
        "В созыве пока нет ни одного члена — сначала внесите состав.": "The term has no members yet — enter the roster first.",
        "Состав применён к правам доступа: обновлено записей — {count}.": "Composition applied to system access: records updated — {count}.",
        "Человек добавлен в состав созыва.": "Person added to the term's roster.",
        "Этот человек уже внесён в состав созыва.": "This person is already on the term's roster.",
        "Запись изменена.": "Entry updated.",
        "Человек убран из состава созыва.": "Person removed from the term's roster.",
        "Созыв правления добавлен. Теперь внесите его состав.": "Board term added. Now enter its roster.",
        "Укажите протокол общего собрания, которым избран новый созыв.": "Select the general meeting protocol that elected the new term.",
        "Созыв закрыт.": "Term closed.",
        "Ревизионная комиссия добавлена. Теперь внесите её состав.": "Revision commission added. Now enter its roster.",
        "Укажите протокол общего собрания, которым избрана комиссия.": "Select the general meeting protocol that elected the commission.",
        "Созыв правления": "Board term",
        "Избрана протоколом от": "Elected by the protocol dated",
        "← Все составы": "← All compositions",
        "Закрыть состав": "Close composition",
        "Закрыть этот состав досрочно?": "Close this composition early?",
        "Членство в ревизионной комиссии на права входа в систему не влияет — это только запись о составе для истории и уставных документов.":
            "Membership in the revision commission does not affect system access — it's only a record of composition for history and bylaws documentation.",
        "сейчас в правлении": "currently on the board",
        "Добавить в состав комиссии": "Add to the commission's roster",
        "По уставу ревизионная комиссия обычно должна быть независима от правления.": "Under typical bylaws, the revision commission should be independent of the board.",
        "председатель комиссии": "commission chair",
        "Председатель комиссии (снимет флаг с текущего, если был)": "Commission chair (will unset the flag from the current one, if any)",
        "Человек добавлен в состав комиссии.": "Person added to the commission's roster.",
        "Этот человек уже внесён в состав комиссии.": "This person is already on the commission's roster.",
        "Обратите внимание: этот человек сейчас числится в действующем составе правления — по уставу ревизионная комиссия обычно должна быть независима от правления. Запись всё равно добавлена, проверьте по вашему уставу.":
            "Note: this person currently belongs to the active board composition — under typical bylaws, the revision commission should be independent of the board. The entry was added anyway; please check your bylaws.",
        "Человек убран из состава комиссии.": "Person removed from the commission's roster.",
        "Состав ревизионной комиссии закрыт.": "Revision commission composition closed.",
        "Рекомендуемый способ формирования состава правления — страница": "The recommended way to build the board's composition is the",
        "«Правление»": "“Board” page",
        "(состав привязан к протоколу общего собрания). Флаги ниже — точечная ручная правка, минуя протокол.":
            "(composition is tied to the general meeting protocol). The flags below are a manual, ad-hoc override that bypasses the protocol.",

        # --- Бухгалтер назначается председателем отдельно от созывов правления ---
        "Бухгалтера назначает председатель — это не выборная общим собранием должность, поэтому вне созывов и без протокола. Бухгалтер не обязан быть членом правления — может быть на аутсорсе, для входа в систему достаточно завести ему учётную запись в разделе «Учётные записи».":
            "The accountant is appointed by the chairman — it isn't a position elected by the general meeting, so it sits outside board terms and needs no protocol. The accountant doesn't have to be a board member — they can be outsourced; to give them system access, just create a user account for them in “User Accounts”.",
        "Бухгалтер не назначен.": "No accountant appointed.",
        "Снять": "Remove",
        "Снять с должности бухгалтера?": "Remove this person as accountant?",
        "Назначить": "Appoint",
        "Назначить бухгалтера": "Appoint accountant",
        "Не обязательно член правления — можно назначить любого человека из справочника, в т.ч. стороннего (аутсорс).":
            "Doesn't have to be a board member — you can appoint anyone from the directory, including an outside person (outsourced).",
        "Бухгалтер назначен.": "Accountant appointed.",
        "Бухгалтер снят с должности.": "Accountant removed.",
        "Роль в правлении": "Board role",
        "Применить этот состав к правам доступа? У людей, не входящих в список, права правления/председателя будут сняты; у входящих — выставлены по списку. Назначение бухгалтера этим действием не затрагивается.":
            "Apply this composition to system access? People not on the list will have their board/chairman access revoked; people on the list will get it set accordingly. The accountant appointment is not affected by this action.",

        # penalty (automatic late-fee accrual)
        "Автоматическое начисление пени": "Automatic late-fee accrual",
        "Срок оплаты взносов": "Dues payment deadline",
        "Срок оплаты взносов — день": "Dues payment deadline — day",
        "Срок оплаты взносов — месяц": "Dues payment deadline — month",
        "Единая по уставу дата в году, после которой на неоплаченные взносы начинает начисляться пеня.":
            "The single yearly date set by the bylaws, after which a late fee starts accruing on unpaid dues.",
        "В этом году": "This year",
        "Пеня начисляется на начисления, не оплаченные к этой дате. Изменить срок можно в реквизитах кооператива.":
            "The late fee accrues on charges not paid by this date. Change the deadline in the cooperative's legal details.",
        "Срок оплаты не настроен.": "Payment deadline is not set.",
        "Задать в реквизитах": "Set it in the legal details",
        "Рассчитать пеню": "Calculate the late fee",
        "Дата расчёта": "Calculation date",
        "Пеня считается по эту дату включительно. По умолчанию — сегодня.": "The late fee is calculated through this date, inclusive. Defaults to today.",
        "Начислить пеню": "Accrue the late fee",
        "Ключевая ставка ЦБ РФ": "Bank of Russia key rate",
        "Ставка, %": "Rate, %",
        "Источник": "Source",
        "вручную": "manual",
        "Удалить эту запись ставки?": "Delete this rate entry?",
        "Ставка ещё не загружена.": "No rate has been loaded yet.",
        "Загрузить с cbr.ru": "Load from cbr.ru",
        "С даты": "From date",
        "По дату": "To date",
        "Загрузить": "Load",
        "Внести вручную": "Enter manually",
        "Если сайт ЦБ недоступен — можно завести ставку самостоятельно, по данным cbr.ru.":
            "If the Bank of Russia site is unavailable, you can enter the rate yourself, based on cbr.ru data.",
        "Пеня начислена": "Late fee accrued",
        "Результат начисления пени на": "Late-fee accrual result for",
        "Счёт пени": "Late-fee account",
        "Год начисления": "Charge year",
        "Сумма пени": "Late-fee amount",
        "Новой просрочки не найдено.": "No new overdue days found.",
        "Итого": "Total",
        "Пропущено — нет счёта пени": "Skipped — no late-fee account",
        "Назад": "Back",
        "Некорректные даты.": "Invalid dates.",
        "Не удалось загрузить ставку с cbr.ru: {error}. Можно внести значение вручную ниже.":
            "Could not load the rate from cbr.ru: {error}. You can enter it manually below.",
        "ЦБ РФ не вернул ни одной записи за указанный период.": "The Bank of Russia returned no records for the given period.",
        "Загружено записей ключевой ставки: {n}.": "Key rate records loaded: {n}.",
        "Ставка на {date} сохранена вручную.": "Rate for {date} saved manually.",
        "Запись ставки удалена.": "Rate entry deleted.",
        "Не задан срок оплаты взносов — укажите день и месяц в реквизитах кооператива.":
            "The dues payment deadline is not set — specify the day and month in the cooperative's legal details.",
        "Нет ни одной записи ключевой ставки ЦБ РФ — загрузите с cbr.ru или внесите вручную.":
            "There isn't a single Bank of Russia key rate record — load it from cbr.ru or enter it manually.",
        "Начислено пени: {n} на сумму {total} ₽.": "Late fees accrued: {n} for a total of {total} ₽.",
        "Новой просрочки не найдено — начислять нечего.": "No new overdue days found — nothing to accrue.",
        "Пропущено (нет счёта пени — заведите вид взноса с is_penalty и тем же кодом счёта): {n}.":
            "Skipped (no late-fee account — set up a fee type with is_penalty and the same account code): {n}.",
        "Страницы": "Pages",
        "Сжать историю": "Compact history",
        "Убрать записи, не меняющие значение по сравнению с предыдущей — оставить только даты фактических изменений ставки.":
            "Remove entries that don't change the value compared to the previous one — keep only the dates of actual rate changes.",
        "Убрано избыточных (без изменения значения): {n}.": "Removed redundant entries (no value change): {n}.",
        "Убрано избыточных записей: {n}.": "Removed redundant entries: {n}.",
        "Избыточных записей не найдено.": "No redundant entries found.",
        "Пеня по сегодняшний день пересчитывается автоматически при каждом открытии этой страницы или панели кооператива — отдельно ничего нажимать не нужно.":
            "The late fee through today is recalculated automatically every time this page or the cooperative dashboard is opened — no need to click anything separately.",
        "Пересчитать на дату": "Recalculate as of a date",
        "По сегодняшний день пеня уже пересчитана автоматически. Эта форма — для пересчёта на другую (например, прошлую) дату, если нужно для отчёта или печати документов.":
            "The late fee through today is already recalculated automatically. Use this form to recalculate as of a different (e.g. past) date, for a report or printed documents.",
        "Пеня считается по эту дату включительно.": "The late fee is calculated through this date, inclusive.",
        "Пересчитать": "Recalculate",

        # electronic voting
        "Голосования": "Voting",
        "Электронные голосования": "Electronic voting",
        "Новое голосование": "New vote",
        "Описание/контекст": "Description / context",
        "Тип голосования": "Voting type",
        "заочное": "absentee",
        "очно-заочное": "in-person and absentee",
        "Связанное очное собрание": "Related in-person meeting",
        "без привязки": "not linked",
        "Для очно-заочного — собрание, очную часть которого это голосование дополняет.":
            "For in-person-and-absentee voting — the meeting whose in-person part this vote supplements.",
        "Начало приёма бюллетеней": "Ballot submission opens",
        "Окончание приёма бюллетеней": "Ballot submission closes",
        "Кворум — правило кооператива, не настраивается за голосование: правомочно, только если приняло участие больше половины голосов кооператива.":
            "Quorum is a fixed cooperative rule, not configurable per vote: the vote is valid only if more than half of the cooperative's votes participated.",
        "Создать (черновик)": "Create (draft)",
        "После создания добавьте вопросы повестки — голосование останется черновиком, пока вы явно его не откроете.":
            "After creating, add agenda questions — the vote stays a draft until you explicitly open it.",
        "напр. Голосование по смете на 2027 год": "e.g. Vote on the 2027 budget",
        "Дата окончания должна быть позже даты начала.": "The end date must be later than the start date.",
        "Голосование создано (черновик) — теперь добавьте вопросы повестки.":
            "Vote created (draft) — now add agenda questions.",
        "Голосование не найдено.": "Vote not found.",
        "Это голосование ещё не открыто.": "This vote is not open yet.",
        "Голосовать могут собственники гаражей — у вас нет доли ни в одном гараже, доступен только просмотр.":
            "Only garage owners can vote — you don't own a share in any garage, view-only access.",
        "Приём бюллетеней до": "Ballots accepted until",
        "Ваш голос": "Your vote",
        "Голосований пока нет.": "No votes yet.",
        "черновик": "draft",
        "идёт приём голосов": "accepting ballots",
        "закрыто": "closed",
        "голос подан": "voted",
        "не голосовали": "haven't voted",
        "← ко всем голосованиям": "← back to all votes",
        "приём бюллетеней": "ballots accepted",
        "собрание от": "meeting on",
        "Явка / кворум": "Turnout / quorum",
        "Проголосовало (по весу)": "Voted (by weight)",
        "Кворум": "Quorum",
        "Кворум (>50% голосов кооператива)": "Quorum (>50% of the cooperative's votes)",
        "достигнут": "reached",
        "не достигнут — решения не могут быть приняты": "not reached — decisions cannot be approved",
        "Удалить вопрос?": "Delete this question?",
        "порог принятия": "approval threshold",
        "от всех голосов кооператива, при наличии кворума": "of all cooperative votes, given quorum is met",
        "ваш голос": "your vote",
        "за": "for",
        "против": "against",
        "воздержался": "abstained",
        "возд.": "abst.",
        "решение принято": "decision passed",
        "решение не принято": "decision did not pass",
        "Повестка пока пуста.": "The agenda is empty so far.",
        "Добавить вопрос повестки": "Add an agenda question",
        "Доля «за» от всех голосов кооператива — 0.5 = простое большинство, 0.6667 ≈ 2/3.":
            "Share of \u00abfor\u00bb out of all cooperative votes — 0.5 = simple majority, 0.6667 \u2248 2/3.",
        "Текст вопроса": "Question text",
        "Порог принятия": "Approval threshold",
        "Добавить вопрос": "Add question",
        "Открыть голосование": "Open the vote",
        "Закрыть голосование": "Close the vote",
        "Закрыть голосование и зафиксировать результаты?": "Close the vote and finalize the results?",
        "Изменить голос": "Change your vote",
        "Проголосовать": "Vote",
        "Подписанный протокол ещё не прикреплён.": "The signed protocol hasn't been attached yet.",
        "Прикрепить": "Attach",
        "Добавлять вопросы можно только пока голосование не открыто.":
            "Questions can only be added while the vote hasn't been opened yet.",
        "Вопрос добавлен.": "Question added.",
        "Удалять вопросы можно только пока голосование не открыто.":
            "Questions can only be deleted while the vote hasn't been opened yet.",
        "Вопрос удалён.": "Question deleted.",
        "Голосование уже открыто или закрыто.": "The vote is already open or closed.",
        "Нельзя открыть голосование без вопросов повестки.": "Can't open a vote with no agenda questions.",
        "Голосование открыто — члены кооператива теперь могут голосовать.":
            "The vote is open — cooperative members can now vote.",
        "Закрыть можно только открытое голосование.": "Only an open vote can be closed.",
        "Голосование закрыто, результаты зафиксированы.": "The vote is closed, results are finalized.",
        "Не удалось сохранить файл протокола.": "Couldn't save the protocol file.",
        "Протокол голосования «{title}»": "Voting protocol \u00ab{title}\u00bb",
        "Протокол прикреплён.": "Protocol attached.",
        "Голосование": "Voting",
        "Голосовать могут только собственники гаражей — у вас нет доли ни в одном гараже.":
            "Only garage owners can vote — you don't own a share in any garage.",
        "Приём бюллетеней по этому голосованию сейчас закрыт.": "Ballot submission for this vote is currently closed.",
        "Ответьте на все вопросы повестки перед отправкой бюллетеня.": "Answer every agenda question before submitting your ballot.",
        "Бюллетень принят. Вы можете изменить голос, пока приём бюллетеней открыт.":
            "Ballot accepted. You can change your vote while ballot submission is still open.",
        "Ваш вес голоса (сумма долей владения по вашим гаражам)": "Your voting weight (sum of your ownership shares across your garages)",
        "За": "For",
        "Против": "Against",
        "Воздержался": "Abstain",
        "Отправить бюллетень": "Submit ballot",
        "Пока приём бюллетеней открыт, вы можете вернуться и изменить свой голос — учитывается последняя подача.":
            "While ballot submission is open, you can come back and change your vote — the latest submission counts.",

        # electronic voting — IN_PERSON / manual ballot recording (ранее не переведено, аудит i18n)
        "Дата голосования": "Voting date",
        "Протокол с результатами": "Protocol with results",
        "Обязателен для очного голосования — это единственный источник результатов.":
            "Required for in-person voting — it's the only source of results.",
        "Зафиксировать голосование": "Record the vote",
        "Решение уже принято на собрании — электронная повестка/бюллетени не заводятся, результаты фиксируются только приложенным протоколом.":
            "The decision has already been made at the meeting — no electronic agenda or ballots are created; results are recorded solely via the attached protocol.",
        "очное": "in-person",
        "Это очное голосование — решение принято на собрании, электронная повестка не заводится. Результаты зафиксированы в протоколе ниже.":
            "This is an in-person vote — the decision was made at the meeting, no electronic agenda is created. Results are recorded in the protocol below.",
        "Вес голоса": "Voting weight",
        "Голоса по вопросам": "Votes by question",
        "Ручная запись очных голосов": "Manual recording of in-person ballots",
        "Для членов, проголосовавших очно на собрании (на бумаге) — их голос не попадает в систему сам, отметьте его здесь. Сам член кооператива сможет переголосовать электронно позже — учитывается последняя подача.":
            "For members who voted in person at the meeting (on paper) — their vote doesn't enter the system on its own, record it here. The member can still re-vote electronically later — the latest submission counts.",
        "Отметить голос": "Record vote",
        "Нет ни одного человека с долей владения в гараже.": "No one owns a share in any garage.",
        "дата голосования": "voting date",
        "Запись голоса за члена кооператива": "Recording a vote on behalf of a member",
        "Вы вносите голос от имени другого человека — используется только для очной части очно-заочного голосования (бумажные бюллетени с собрания). Сам член кооператива сможет переголосовать электронно позже, пока приём открыт — учитывается последняя подача.":
            "You're recording a vote on someone else's behalf — used only for the in-person part of an in-person-and-absentee vote (paper ballots from the meeting). The member can still re-vote electronically later while submission is open — the latest submission counts.",
        "вес голоса": "voting weight",
        "Можно отметить не все вопросы сразу — незаполненные останутся как есть, к ним можно вернуться позже.":
            "You don't have to record all questions at once — unfilled ones stay as they are, you can come back to them later.",
        "Записать голос": "Record vote",
        "Укажите дату голосования.": "Please enter the voting date.",
        "Для очного голосования обязательно приложите протокол с результатами.": "For an in-person vote, attaching the results protocol is required.",
        "Очное голосование зафиксировано, протокол прикреплён.": "In-person vote recorded, protocol attached.",
        "Ручная запись голоса доступна только для очно-заочного голосования.": "Manual ballot recording is available only for in-person-and-absentee voting.",
        "Записывать голоса можно только пока голосование открыто.": "Ballots can only be recorded while the vote is open.",
        "У этого человека нет доли ни в одном гараже — голосовать он не может.": "This person doesn't own a share in any garage — they cannot vote.",
        "Отметьте хотя бы один вопрос повестки.": "Mark at least one agenda question.",
        "Голос члена кооператива «{name}» зафиксирован.": "The vote for member «{name}» has been recorded.",

        # setup wizard (мастер первоначальной настройки)
        "Мастер первоначальной настройки": "First-launch setup wizard",
        "Проведите кооператив через первоначальное заполнение данными. Шаги можно проходить в любом порядке и возвращаться сюда в любой момент — ничего не потеряется, статус каждого шага считается по уже введённым данным.":
            "Walk the cooperative through its initial data setup. Steps can be done in any order and revisited any time — nothing is lost, each step's status is computed from the data already entered.",
        "Все шаги первоначальной настройки заполнены. Мастер можно больше не открывать — все дальнейшие изменения делаются на обычных страницах кооператива.":
            "All setup steps are complete. You don't need to open the wizard again — further changes are made on the cooperative's regular pages.",
        "заполнено": "done",
        "не заполнено": "not done",
        "Открыть мастер настройки": "Open the setup wizard",
        "Первоначальная настройка ещё не завершена — часть данных не заполнена.": "Initial setup isn't finished yet — some data is still missing.",
        "← Мастер настройки": "← Setup wizard",
        "Карточка кооператива": "Cooperative card",
        "Карточка кооператива сохранена.": "Cooperative card saved.",
        "Название, ИНН/КПП/ОГРН, адреса, площади.": "Name, tax IDs, addresses, areas.",
        "Единый срок оплаты взносов — день": "Common dues due date — day",
        "Единый срок оплаты взносов — месяц": "Common dues due date — month",
        "Остальные реквизиты (проценты банка, стандартную площадь под гараж и т.п.) можно уточнить позже в разделе «Реквизиты».":
            "The remaining details (bank fee percentage, standard garage land area, etc.) can be filled in later in the «Legal details» section.",
        "Сохранить и вернуться в мастер": "Save and return to the wizard",
        "Открыть полную страницу реквизитов": "Open the full legal details page",
        "Расчётный счёт (р/с)": "Checking account number",
        "Корреспондентский счёт (к/с)": "Correspondent account",
        "Текущий баланс": "Current balance",
        "Р/с": "Acc.",
        "Основной счёт (используется в реквизитах ПД-4)": "Primary account (used in PD-4 payment slip details)",
        "Добавить и вернуться в мастер": "Add and return to the wizard",
        "Хотя бы один расчётный счёт для реквизитов на ПД-4.": "At least one bank account, for PD-4 payment slip details.",
        "Поставщик электроэнергии сохранён.": "Electricity supplier saved.",
        "Контрагент-поставщик электроэнергии создан и привязан.": "Electricity supplier counterparty created and linked.",
        "Контрагент-поставщик электроэнергии, хотя бы один.": "At least one electricity supplier counterparty.",
        "Организация, у которой кооператив покупает электроэнергию по общему (вводному) счётчику — на неё будут заводиться расходы при внесении показаний.":
            "The organization the cooperative buys electricity from via the common (master) meter — expenses will be recorded against it when readings are entered.",
        "Текущий поставщик:": "Current supplier:",
        "Наименование": "Name",
        "Создать и привязать": "Create and link",
        "Или выбрать из уже существующих": "Or pick from existing ones",
        "выберите контрагента": "select a counterparty",
        "Тариф на электроэнергию": "Electricity tariff",
        "Ставка, ₽/кВт·ч": "Rate, ₽/kWh",
        "Действующий тариф, ₽/кВт·ч.": "Current tariff, ₽/kWh.",
        "Счётчик кооператива": "Cooperative meter",
        "Счётчик кооператива и начальные показания": "Cooperative meter and initial readings",
        "Общий (вводный) счётчик кооператива — начальные показания.": "The cooperative's common (master) meter — initial readings.",
        "Общий (вводный) счётчик кооператива — то, что реально приходит от энергосбытовой компании, отдельно от счётчиков на гаражах. Здесь вносятся самые первые показания по нему; сумма от них не считается — не с чем сравнивать. Все последующие показания вносятся уже на обычной странице «Электроэнергия».":
            "The cooperative's common (master) meter — what actually comes from the utility company, separate from garage meters. This is where the very first reading is entered; no amount is calculated for it — there's nothing to compare it to. Every later reading is entered on the regular «Electricity» page.",
        "Сначала добавьте тариф на электроэнергию — без него нельзя рассчитать суммы по будущим показаниям.":
            "Add an electricity tariff first — without it, amounts for future readings can't be calculated.",
        "Сначала добавьте тариф на электроэнергию — без него нельзя рассчитать сумму по показаниям.":
            "Add an electricity tariff first — without it, the amount for this reading can't be calculated.",
        "Перейти к тарифу": "Go to the tariff step",
        "Начальные показания уже внесены. Дальнейший ввод — на странице «Электроэнергия».": "Initial readings are already entered. Enter further readings on the «Electricity» page.",
        "Открыть «Электроэнергия»": "Open «Electricity»",
        "Начальные показания (внесены через мастер первого запуска)": "Initial reading (entered via the first-launch setup wizard)",
        "Внести и вернуться в мастер": "Enter and return to the wizard",
        "Начальные показания общего счётчика внесены.": "Initial common meter reading entered.",
        "Всего в базе:": "Total in the database:",
        "Добавить одного человека": "Add one person",
        "Полная форма — с паспортными данными, адресами, телефонами.": "Full form — with passport details, addresses, phone numbers.",
        "Импорт из CSV": "Import from CSV",
        "Файл в кодировке UTF-8, разделитель колонок — запятая, первая строка — заголовок (пропускается). Дублирующиеся по ФИО строки (без учёта регистра) — пропускаются.":
            "UTF-8 file, comma-separated columns, first row is a header (skipped). Rows that duplicate an existing full name (case-insensitive) are skipped.",
        "Колонки по порядку:": "Columns, in order:",
        "Обязательна только первая (ФИО), остальные можно оставлять пустыми.": "Only the first one (full name) is required; the rest can be left blank.",
        "Скачать пример CSV": "Download a sample CSV",
        "Выберите CSV-файл.": "Select a CSV file.",
        "Не удалось прочитать файл — сохраните его в кодировке UTF-8.": "Couldn't read the file — please save it as UTF-8.",
        "Импортировать": "Import",
        "Импорт людей завершён: добавлено {created}, пропущено дублей {dup}, пропущено с ошибками {inv}.":
            "People import finished: added {created}, skipped as duplicates {dup}, skipped with errors {inv}.",
        "Последние добавленные": "Recently added",
        "Члены кооператива и контактные лица — вручную или импортом из CSV.": "Cooperative members and contacts — added manually or imported from CSV.",
        "Добавить один гараж": "Add one garage",
        "Полная форма — с площадью, кадастровыми номерами, собственниками и фото.": "Full form — with area, cadastral numbers, owners, and photos.",
        "Файл в кодировке UTF-8, разделитель колонок — запятая, первая строка — заголовок (пропускается). Числа — с точкой в качестве десятичного разделителя. Если у гаража несколько собственников, добавьте отдельную строку с тем же номером гаража и другим ФИО собственника — площадь и остальные поля гаража берутся из первой встреченной строки с этим номером. Собственник, которого нет в базе, будет создан автоматически по ФИО.":
            "UTF-8 file, comma-separated columns, first row is a header (skipped). Numbers use a dot as the decimal separator. If a garage has several owners, add a separate row with the same garage number and a different owner name — the area and the rest of the garage's fields are taken from the first row seen with that number. An owner not yet in the database is created automatically by full name.",
        "Обязательны номер гаража и площадь; доля собственника по умолчанию — 1 (100%).": "Garage number and area are required; owner share defaults to 1 (100%).",
        "Импорт гаражей завершён: создано гаражей {garages}, записей о собственниках добавлено {owners}, пропущено строк с ошибками {inv}.":
            "Garage import finished: {garages} garages created, {owners} ownership records added, {inv} rows skipped with errors.",
        "Гаражи с указанием собственников и их долей — вручную или импортом из CSV.": "Garages with owners and their shares — added manually or imported from CSV.",
        "Состав правления": "Board composition",
        "Нынешний состав правления и председатель.": "The current board composition and chairman.",
        "Ваша учётная запись пока не привязана ни к одному человеку в базе — без этого применение состава правления не затронет права входа в этой сессии.":
            "Your account isn't linked to a person in the database yet — without that, applying the board composition won't affect this session's login rights.",
        "Ваше ФИО": "Your full name",
        "Создать карточку и привязать": "Create the record and link it",
        "Укажите ФИО.": "Enter a full name.",
        "К вашей учётной записи уже привязан человек.": "Your account is already linked to a person.",
        "Карточка человека создана и привязана к вашей учётной записи.": "The person record was created and linked to your account.",
        "Ни одного созыва правления ещё нет. На старте системы избирать было ещё не из чего — создайте первый созыв как есть, протокол общего собрания указывать не обязательно. Для всех последующих созывов (переизбрания) протокол уже потребуется.":
            "There's no board term yet. At the very start there was nothing to elect from — just create the first term as it stands, a general meeting protocol isn't required. Every later term (re-election) will require one.",
        "Создать текущий созыв": "Create the current term",
        "Созыв правления создан. Теперь внесите его состав.": "Board term created. Now enter its members.",
        "Действующий созыв правления уже есть.": "There's already an active board term.",
        "Действующий созыв правления уже создан — состав и применение прав вносятся на его странице.":
            "An active board term already exists — its members and rights are managed on its own page.",
        "Открыть состав созыва": "Open the term's member list",
        "электрика": "electrical",
        "сегодня, если не указано": "today, if left blank",

        # configurable CSV import format
        "Настроить формат": "Configure format",
        "Настройка формата CSV-файла": "CSV file format setup",
        "Отметьте, какие колонки есть в вашем файле, и укажите их порядковый номер (позицию) в файле слева направо, начиная с 1. Лишние (неотмеченные) колонки в файле допускаются — их можно оставить без номера. Формат сохраняется и используется и для скачиваемого образца, и при следующих импортах.":
            "Check the columns present in your file and give each its position (left to right, starting at 1). Extra unchecked columns in the file are fine — just leave them without a number. The format is saved and reused for both the downloadable sample and future imports.",
        "Колонка": "Column",
        "Позиция в файле": "Position in file",
        "обязательна": "required",
        "Сохранить формат": "Save format",
        "Текущий формат, колонки по порядку:": "Current format, columns in order:",
        "Изменить состав и порядок колонок — кнопкой «Настроить формат» выше.": "Change the set and order of columns with the «Configure format» button above.",
        "Формат CSV для импорта людей сохранён.": "CSV import format for people saved.",
        "Формат CSV для импорта гаражей сохранён.": "CSV import format for garages saved.",
        "В формате обязательно должна быть колонка «ФИО».": "The format must include the «Full name» column.",
        "В формате обязательно должны быть колонки «Номер гаража», «Площадь, м²» и «ФИО собственника».":
            "The format must include the «Garage number», «Area, m²» and «Owner's full name» columns.",

        # bank API integration
        "API банка": "Bank API",
        "Не используется — баланс вручную": "Not used — enter balance manually",
        "Сбербанк (СберБизнес)": "Sberbank (SberBusiness)",
        "ВТБ (интеграция ещё не реализована)": "VTB (integration not implemented yet)",
        "Т-Банк (интеграция ещё не реализована)": "T-Bank (integration not implemented yet)",
        "Автоматическое получение баланса, выписки и работа с реестрами начислений/платежей — пока только для Сбербанка.":
            "Automatic balance, statement, and charge/payment registry sync — Sberbank only for now.",
        "Реквизиты подключения (client_id и т.п.) задаются отдельно, кнопкой «Настроить API» после сохранения счёта.":
            "Connection credentials (client_id etc.) are set separately, with the «Configure API» button after saving the account.",
        "не используется": "not used",
        "выбран, не реализовано": "selected, not implemented",
        "ошибка": "error",
        "Выписка": "Statement",
        "Реестр начислений": "Charge registry",
        "Реестр платежей": "Payment registry",
        "Обновить баланс": "Update balance",
        "Настроить API": "Configure API",
        "Настройка API": "API settings",
        "Интеграция для этого банка ещё не реализована — реквизиты можно сохранить заранее, но синхронизация работать не будет.":
            "Integration for this bank isn't implemented yet — you can save credentials in advance, but sync won't work.",
        "Тестовый контур банка (песочница)": "Bank sandbox environment",
        "оставьте пустым, чтобы не менять": "leave blank to keep unchanged",
        "Секрет уже сохранён — заполните заново только чтобы заменить его.": "A secret is already saved — fill this in again only to replace it.",
        "ID организации в банке": "Organization ID at the bank",
        "Заполнять, только если отличается от ИНН кооператива.": "Fill in only if different from the cooperative's INN.",
        "Номер счёта для запросов к банку": "Account number for bank API requests",
        "Заполнять, только если отличается от расчётного счёта выше.": "Fill in only if different from the checking account above.",
        "Последняя ошибка синхронизации": "Last sync error",
        "Баланс синхронизирован": "Balance last synced",
        "Выписка синхронизирована": "Statement last synced",
        "Выписка по счёту": "Account statement",
        "Дата с": "Date from",
        "Дата по": "Date to",
        "Показать": "Show",
        "Загрузить из банка": "Fetch from bank",
        "Номер документа": "Document number",
        "№ документа": "Doc. no.",
        "зачисление": "credit",
        "списание": "debit",
        "За выбранный период операций нет.": "No transactions for the selected period.",
        "Реестр — это текущая непогашенная задолженность по всем лицевым счетам (взносы, налоги, электричество), отправленная в банк: плательщики видят её и могут оплатить в приложении банка по своему лицевому счёту.":
            "The registry is the current outstanding debt across all personal accounts (dues, tax, electricity), sent to the bank: payers see it and can pay in the bank's app using their account number.",
        "Сейчас в долгах": "Currently in debt",
        "лицевых счетов на сумму": "personal accounts, totaling",
        "например, август 2026": "e.g. August 2026",
        "Отправить реестр в банк": "Send registry to bank",
        "Отправлен": "Sent",
        "Начислений": "Charges",
        "Комментарий банка": "Bank comment",
        "Обновить статус": "Refresh status",
        "Реестры ещё не отправлялись.": "No registries sent yet.",
        "Нет непогашенной задолженности для отправки.": "No outstanding debt to send.",
        "Укажите период реестра.": "Specify the registry period.",
        "Реестр начислений отправлен в банк.": "Charge registry sent to the bank.",
        "Не удалось отправить реестр начислений: {error}": "Failed to send the charge registry: {error}",
        "Статус реестра обновлён.": "Registry status updated.",
        "Не удалось обновить статус: {error}": "Failed to refresh status: {error}",
        "Платежи, поступившие в банк по реестру начислений. Разнести запись — значит создать по ней платёж на найденном лицевом счёте и зачесть его в счёт задолженности (обычный порядок распределения, как при ручном вводе платежа).":
            "Payments received by the bank against the charge registry. Allocating an entry creates a payment on the matched personal account and applies it against the debt (the same allocation as a manually entered payment).",
        "Запросить из банка": "Fetch from bank",
        "разнесён": "allocated",
        "не разнесён": "not allocated",
        "Разнести": "Allocate",
        "Записей реестра платежей пока нет.": "No payment registry entries yet.",
        "Эта запись уже разнесена.": "This entry has already been allocated.",
        "В записи реестра нет номера лицевого счёта — разнесите платёж вручную.": "This registry entry has no account number — allocate the payment manually.",
        "Лицевой счёт «{number}» не найден в системе.": "Account number «{number}» wasn't found in the system.",
        "Импорт из реестра платежей банка (id {id})": "Imported from bank payment registry (id {id})",
        "Платёж разнесён.": "Payment allocated.",
        "Не удалось выполнить запрос — проверьте соединение и попробуйте ещё раз.":
            "Couldn't complete the request — check your connection and try again.",
        "Реестр платежей обновлён: {n} новых записей.": "Payment registry updated: {n} new entries.",
        "Не удалось получить реестр платежей: {error}": "Failed to fetch the payment registry: {error}",
        "Для этого счёта не настроен или не поддерживается реестр платежей.": "This account has no configured or supported payment registry.",
        "Для этого счёта не настроен или не поддерживается реестр начислений.": "This account has no configured or supported charge registry.",
        "Для этого счёта не настроено или не поддерживается автоматическое обновление баланса.": "This account has no configured or supported automatic balance update.",
        "Для этого счёта не настроено или не поддерживается автоматическая выписка.": "This account has no configured or supported automatic statement.",
        "Не удалось получить баланс из банка: {error}": "Failed to fetch balance from the bank: {error}",
        "Баланс обновлён из банка.": "Balance updated from the bank.",
        "Не удалось получить выписку из банка: {error}": "Failed to fetch the statement from the bank: {error}",
        "Выписка обновлена: {n} новых операций.": "Statement updated: {n} new transactions.",
        "API банка отключён для этого счёта.": "Bank API disabled for this account.",
        "Настройки API банка сохранены.": "Bank API settings saved.",
        "Невозможно обновить статус этого реестра.": "Can't refresh the status of this registry.",
        "Скачать файл реестра (CP1251)": "Download registry file (CP1251)",
        "на случай, если автоматическая отправка через API недоступна — файл можно загрузить в СберБизнес Онлайн вручную":
            "in case automatic sending via API isn't available — the file can be uploaded to SberBusiness Online manually",
        "Или загрузить файл(ы) реестра вручную (CP1251)": "Or upload registry file(s) manually (CP1251)",
        "Загрузить файлы": "Upload files",
        "на случай, если запрос через API недоступен — файлы, как их отдаёт СберБизнес Онлайн (кодировка Windows-1251); можно выбрать сразу несколько":
            "in case the API request isn't available — the files as provided by SberBusiness Online (Windows-1251 encoding); you can select several at once",
        "Файл реестра не выбран.": "No registry file selected.",
        "Ни в одном из {n} файлов не найдено ни одной корректной строки — проверьте, что файлы в кодировке Windows-1251.":
            "None of the {n} files had any valid rows — check that they're encoded as Windows-1251.",
        "Пропущено файлов без корректных строк: {n} ({files}).": "Files skipped for having no valid rows: {n} ({files}).",
        "Реестр платежей обновлён из {n} файл(ов): {added} новых записей.": "Payment registry updated from {n} file(s): {added} new entries.",
        "Не удалось получить реестр платежей через API: {error}. Можно скачать файл в СберБизнес Онлайн и загрузить его вручную ниже.":
            "Failed to fetch the payment registry via API: {error}. You can download the file from SberBusiness Online and upload it manually below.",
        "Клиентский сертификат (mTLS)": "Client certificate (mTLS)",
        "Банк требует клиентский сертификат для самого соединения — отдельно от client_id/secret. Выдаётся в личном кабинете Sber API в формате .pfx/.p12 с паролем; загрузите его здесь, пароль не сохраняется, используется один раз для конвертации.":
            "The bank requires a client certificate for the connection itself — separate from client_id/secret. It's issued in the Sber API personal cabinet as a .pfx/.p12 file with a password; upload it here — the password isn't stored, it's used once for conversion.",
        "Сертификат загружен.": "Certificate uploaded.",
        "Загрузите новый файл ниже, чтобы заменить.": "Upload a new file below to replace it.",
        "Файл сертификата (.pfx/.p12)": "Certificate file (.pfx/.p12)",
        "Пароль от файла сертификата": "Certificate file password",
        "Также обязательны доверенные сертификаты банка (УЦ Сбера и НУЦ Минцифры) на сервере приложения — переменная окружения SBERBANK_API_CA_BUNDLE, см. .env.example (там же прямые ссылки на архивы с цепочкой сертификатов от банка). Без неё соединение с банком не установится, даже если всё остальное настроено верно.":
            "You'll also need the bank's trusted certificates (Sber CA and the Russian National CA / Mintsifry) on the application server — the SBERBANK_API_CA_BUNDLE environment variable, see .env.example (which also has direct links to the bank's certificate chain archives). Without it, the connection to the bank won't be established even if everything else is configured correctly.",
        "Обязателен — Sber API работает не по client_id/secret напрямую, а по access_token/refresh_token, выданным конкретному пользователю СберБизнес. Получите пару access_token/refresh_token в личном кабинете Sber API («Ключи доступа» → сгенерировать) и вставьте сюда refresh_token — access_token приложению не нужен, он сам обновляется через refresh_token перед каждым обращением к банку.":
            "Required — Sber API doesn't authenticate with client_id/secret alone, but with an access_token/refresh_token pair issued to a specific SberBusiness user. Get an access_token/refresh_token pair in the Sber API personal cabinet («Access keys» → generate) and paste the refresh_token here — the access_token itself isn't needed by the app, it's refreshed automatically before every request to the bank.",
        "Уже сохранён — заполните заново только чтобы заменить.": "Already saved — fill in again only to replace it.",
        "Разнести всё возможное": "Allocate everything possible",
        "повторяет автоматическое распознавание для всех ещё не разнесённых зачислений разом — удобно после добавления нового лицевого счёта":
            "re-runs automatic matching for all not-yet-allocated credits at once — handy after adding a new account",
        "реестр платежей": "payment registry",
        "Сумма сразу за нескольких плательщиков — разносится через реестр платежей, не здесь.":
            "A single amount covering several payers — allocate it via the payment registry, not here.",
        "К реестру платежей": "Go to payment registry",
        "Разнесено: {n} операций.": "Allocated: {n} transactions.",
        "Не удалось разнести ни одной операции — для оставшихся нужен ручной ввод номера счёта.":
            "Couldn't allocate any transactions — the remaining ones need the account number entered manually.",
        "Осталось неразнесённых (нужен ручной ввод номера счёта): {n}.":
            "Still unallocated (need the account number entered manually): {n}.",
        "Не удалось прочитать файл сертификата (.pfx/.p12) — проверьте пароль и формат файла.":
            "Couldn't read the certificate file (.pfx/.p12) — check the password and file format.",
        "Файл сертификата не содержит приватного ключа или самого сертификата.":
            "The certificate file doesn't contain a private key or a certificate.",
        "Формат файлов реестров": "Registry file format",
        "Порядок и состав полей текстового файла реестра — индивидуальны и зависят от конкретного договора с банком. По умолчанию используется формат из реальных образцов файлов; если у вашего подключения он другой — настройте отдельно для реестра начислений и реестра платежей.":
            "The order and set of fields in the registry text file are specific to your bank agreement. The default matches real sample files; if yours differs, configure the charge and payment registry formats separately.",
        "Реестр начислений (исходящий файл)": "Charge registry (outgoing file)",
        "Реестр платежей (входящий файл)": "Payment registry (incoming file)",
        "Настроить": "Configure",
        "Текущие поля по порядку:": "Current fields in order:",
        "Разделитель дробной части суммы:": "Amount decimal separator:",
        "Код услуги/периода:": "Service/period code:",
        "Префикс строки-сводки в конце файла (пропускается при разборе):": "Summary line prefix at the end of the file (skipped when parsing):",
        "Разделитель полей и кодировка — общие для обоих файлов:": "Field delimiter and encoding are shared between both files:",
        "Разделитель дробной части суммы": "Amount decimal separator",
        "Префикс строки-сводки в конце файла": "Summary line prefix at the end of the file",
        "Строки, начинающиеся с этого символа, считаются итоговой сводкой и пропускаются при разборе. Оставьте пустым, если сводки в файле нет.":
            "Lines starting with this character are treated as a summary line and skipped when parsing. Leave empty if the file has no summary line.",
        "Меняются в настройке реестра начислений.": "Changed in the charge registry settings.",
        "Настройка формата реестра начислений": "Charge registry format settings",
        "Настройка формата реестра платежей": "Payment registry format settings",
        "Отметьте, какие поля есть в файле, и укажите их порядковый номер (позицию) в строке слева направо, начиная с 1.":
            "Check which fields are present in the file and set their position in the line, left to right, starting from 1.",
        "Поле": "Field",
        "Позиция": "Position",
        "обязательно": "required",
        "Разделитель полей": "Field delimiter",
        "Кодировка файла": "File encoding",
        "Код услуги/периода": "Service/period code",
        "Подставляется в поле «Код услуги/периода», если оно включено в формат.": "Used for the «Service/period code» field, if it's included in the format.",
        "Оставьте пустым, если сводки в файле нет.": "Leave blank if the file has no summary line.",
        "Настроить формат файла": "Configure file format",
        "Время": "Time",
        "Код отделения": "Branch code",
        "ID терминала": "Terminal ID",
        "Номер операции (для дедупликации)": "Operation number (for deduplication)",
        "Сумма начисления": "Charged amount",
        "Сумма зачисления (за вычетом комиссии)": "Credited amount (net of fee)",
        "Комиссия банка": "Bank fee",
        "Код статуса операции": "Operation status code",
        "В формате реестра начислений обязательны поля «Лицевой счёт», «ФИО плательщика», «Назначение платежа» и «Сумма».":
            "The charge registry format requires the fields «Account number», «Payer full name», «Payment purpose» and «Amount».",
        "В формате реестра платежей обязательны поля «Дата», «Лицевой счёт» и «Сумма начисления».":
            "The payment registry format requires the fields «Date», «Account number» and «Charged amount».",
        "Формат файла реестра начислений сохранён.": "Charge registry file format saved.",
        "Формат файла реестра платежей сохранён.": "Payment registry file format saved.",
        "Погашение": "Settlement",
        "Реестр": "Registry",
        "из реестра": "from registry",
        "из выписки": "from statement",
        "Сопоставить с выпиской": "Match with statement",
        "Зачисления гасят задолженность на лицевом счёте автоматически при загрузке — по номеру счёта, распознанному в назначении платежа («ЛС <номер>»), а если его нет — по совпадению ФИО плательщика или того, за кого платят, включая лиц для связи по гаражу. Полной поступившей суммой, без вычета возможной комиссии банка. Автоматически — только когда совпадение однозначно; в остальных случаях — кнопка «Разнести» ниже.":
            "Credits automatically settle the debt on an account — by the account number recognized in the payment purpose («Acct <number>»), or, failing that, by a match on the payer's full name or the name of whoever is being paid for, including garage contact persons. Using the full amount received, without deducting any bank fee. Automatic only when the match is unambiguous; otherwise use the «Allocate» button below.",
        "Разносить можно только зачисления, не списания.": "Only credits can be allocated, not debits.",
        "Разнесено вручную по выписке банка, операция {uid}": "Manually allocated from the bank statement, transaction {uid}",
        "Автоматически разнесено по выписке банка, операция {uid}": "Automatically allocated from the bank statement, transaction {uid}",
        "Сопоставлено: {direct} прямых + {parametric} параметрических совпадений.":
            "Matched: {direct} direct + {parametric} parametric matches.",
        "Новых совпадений не найдено.": "No new matches found.",
        "Эта операция уже разнесена.": "This transaction has already been allocated.",
        "Выписка обновлена: {n} новых операций, из них {m} автоматически разнесено по лицевым счетам.":
            "Statement updated: {n} new transactions, {m} of them automatically allocated to accounts.",
        "Лицевой счёт «{number}» не найден, и по имени плательщика однозначно определить его тоже не удалось.":
            "Account «{number}» wasn't found, and the payer's name didn't produce an unambiguous match either.",
        "Не удалось определить лицевой счёт по имени плательщика — укажите номер лицевого счёта вручную.":
            "Couldn't determine the account from the payer's name — enter the account number manually.",
        "Не удалось найти лицевой счёт ни по номеру, ни по имени плательщика для этой записи реестра.":
            "Couldn't find an account by number or by payer name for this registry entry.",
        "Зачислено (за вычетом комиссии)": "Credited (net of fee)",
        "комиссия": "fee",

        # error pages / form-input safety net (app/errors.py, error.html)
        "Доступ запрещён": "Access denied",
        "У вас нет прав для просмотра этой страницы.": "You don't have permission to view this page.",
        "Страница не найдена": "Page not found",
        "Такой страницы не существует, либо она была удалена.": "This page doesn't exist, or it has been removed.",
        "Непредвиденная ошибка": "Unexpected error",
        "Что-то пошло не так на сервере. Попробуйте ещё раз или обратитесь в правление.":
            "Something went wrong on the server. Please try again or contact the board.",
        "Сессия формы устарела или недействительна — обновите страницу и попробуйте снова.":
            "The form session has expired or is invalid — refresh the page and try again.",
        "Проверьте правильность заполнения формы — одно из полей заполнено некорректно или не заполнено.":
            "Please check the form — one of the fields is filled in incorrectly or left empty.",
        "На главную": "Go to dashboard",

        # garages/detail.html — meter controls
        "Заменить": "Replace",
        "Изменить параметры": "Edit settings",
        "Изменить прибор учета": "Change the meter",

        # garages.py
        "Счётчик обновлён.": "Meter updated.",

        # cabinet/profile.html
        "Контактные данные": "Contact details",
        "Паспортные данные": "Passport details",
        "Если ваши паспортные данные изменились (смена паспорта, регистрация/прописка) — обновите информацию ниже. Изменения применяются только после одобрения председателя.":
            "If your passport details have changed (new passport, registration/residence permit) — update the information below. Changes apply only after the chairman approves them.",

        # cooperative/view.html
        "площадь кооператива - площадь приватизированных участков": "cooperative area minus privatized plots",
        "дорога + площадь между гаражами": "road + area between garages",
        "24м² + 1,5м² перед гаражом": "24m² + 1.5m² in front of the garage",
        "Сбербанк": "Sberbank",

        # search placeholders
        "Поиск по ФИО, гаражу или счёту...": "Search by name, garage, or account...",
        "Поиск по ФИО, телефону, email...": "Search by name, phone, email...",
        "Поиск по логину, ФИО, роли...": "Search by username, name, role...",
        "Поиск по названию, ИНН, комментарию...": "Search by name, tax ID, comment...",

        # finance/mass_charge.html
        "гаражей выбрано — если ни один не выбран, начисление пойдёт на все гаражи":
            "garages selected — if none are selected, the charge goes to all garages",
        "Выберите стратегию и заполните поля для предварительного расчёта.":
            "Choose a strategy and fill in the fields for a preliminary calculation.",
        "Превью расчёта": "Calculation preview",
        "Округлять сумму в большую сторону, руб.": "Round the amount up, RUB",
        "Не округлять": "Don't round",
        "10": "10",
        "50": "50",
        "100": "100",
        "Площадь кооператива на кадастровой карте": "Cooperative area on the cadastral map",
        "кадастровая стоимость": "cadastral value",
        "Сумма начисления на каждого собственника округляется вверх до указанного значения.":
            "The charge amount for each owner is rounded up to the specified value.",
        "Считается по текущей площади и кадастровой стоимости кооператива — под постройкой и общей территорией, с учётом приватизированных участков и % банка за обслуживание. Вид взноса определяется автоматически («Земельный налог»).":
            "Calculated from the cooperative's current area and cadastral value — under the buildings and shared land, accounting for privatized plots and the bank's service fee %. The fee type is set automatically («Land tax»).",
        "В реквизитах кооператива не заполнены кадастровая площадь и стоимость — расчёт невозможен.":
            "The cooperative's legal details don't have the cadastral area and value filled in — calculation isn't possible.",

        # cooperative/view.html — площади и налоги
        "% банка за зачисление": "Bank fee % on crediting",
        "Из них, площадь общего пользования, м²": "Of which, shared-use area, m²",
        "Площади и налоги": "Areas and taxes",
        "Площадь кооператива (кадастровая), м²": "Cooperative area (cadastral), m²",
        "Полная площадь кооператива (по документам), м²": "Full cooperative area (per documents), m²",
        "Стоимость аренды 1 м² (справочная), ₽/м²": "Rental value per m² (reference), RUB/m²",
        "Стоимость земли (кадастровая), ₽": "Land value (cadastral), RUB",
        "Текущая площадь на кадастровой карте, уменьшается при приватизации.":
            "Current area on the cadastral map, decreases as plots are privatized.",
        "Уменьшается при приватизации.": "Decreases as plots are privatized.",

        # garages/detail.html — доля собственника
        "Доля (0...1, например 0.5 для 50%)": "Share (0...1, e.g. 0.5 for 50%)",
        "Изменить долю": "Edit share",
        "Изменить долю: {name}": "Edit share: {name}",

        # garages.py
        "Доля обновлена.": "Share updated.",

        # finance.py
        "Недостаточно данных для расчёта: заполните текущую площадь и кадастровую стоимость кооператива в его карточке.":
            "Not enough data for the calculation: fill in the cooperative's current area and cadastral value in its card.",

        # search placeholders
        "Поиск по дате или ставке...": "Search by date or rate...",
        "Поиск по счёту, плательщику, назначению...": "Search by account, payer, purpose...",
        "Поиск по действию, пользователю или описанию...": "Search by action, user, or description...",

        # pd4 print
        "Дата платежа": "Payment date",
    }
}

ROLE_LABELS = {
    "ru": {"chairman": "председатель", "board": "член правления", "accountant": "бухгалтер", "member": "член кооператива"},
    "en": {"chairman": "chairman", "board": "board member", "accountant": "accountant", "member": "member"},
}

DOC_TYPE_LABELS = {
    "ru": {
        "charter": "устав", "order": "приказ", "act": "акт",
        "letter": "письмо", "protocol": "протокол",
        "invoice": "счёт", "statement": "выписка", "certificate": "справка",
        "estimate": "смета", "report": "отчёт", "other": "прочее",
    },
    "en": {
        "charter": "charter", "order": "order", "act": "act",
        "letter": "letter", "protocol": "protocol",
        "invoice": "invoice", "statement": "statement", "certificate": "certificate",
        "estimate": "estimate", "report": "report", "other": "other",
    },
}

VOTE_TYPE_LABELS = {
    "ru": {
        "absentee": "заочное", "in_person_and_absentee": "очно-заочное", "in_person": "очное",
    },
    "en": {
        "absentee": "absentee", "in_person_and_absentee": "in-person + absentee", "in_person": "in-person",
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


def vote_type_label(vote_type_value: str) -> str:
    locale = getattr(g, "locale", DEFAULT_LANGUAGE)
    return VOTE_TYPE_LABELS.get(locale, VOTE_TYPE_LABELS["ru"]).get(vote_type_value, vote_type_value)


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
    app.jinja_env.globals["vote_type_label"] = vote_type_label
    app.jinja_env.globals["fmt_date"] = format_date
    app.jinja_env.globals["fmt2"] = fmt2
    app.jinja_env.globals["SUPPORTED_LANGUAGES"] = SUPPORTED_LANGUAGES
    app.jinja_env.globals["LANGUAGE_FLAGS"] = LANGUAGE_FLAGS

    @app.context_processor
    def _inject_locale():
        return {"current_locale": getattr(g, "locale", DEFAULT_LANGUAGE)}

    @app.route("/set-language/<lang>")
    def set_language(lang):
        from flask import redirect, request as req
        if lang in SUPPORTED_LANGUAGES:
            session["lang"] = lang
        return redirect(req.referrer or "/")

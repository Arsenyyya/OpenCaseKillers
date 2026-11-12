# Независимая оценка качества

Demo fixtures проверяют сценарии и инварианты, но не измеряют качество Qwen. Без независимой разметки и реальных результатов `support-agent evaluate` возвращает not_run. Ответы с provider!=internal или synthetic=true не дают production-метрик.

## Формат

Оба файла — JSON до10MB. Gold и results содержат `tickets`: уникальный ticket_id, channel и items. Используйте глобально уникальные IDs с продуктом, например atlas:T-1. Item имеет kind, человеческий canonical need_id и необязательный occurrence_id; по умолчанию occurrence_id равен `ticket_id::kind::need_id`.

`groups` — списки occurrence IDs данного файла. Один occurrence не входит в несколько групп; произвольные ссылки, дубли и чужие result tickets отклоняются. Канал результата должен совпадать с gold.

```json
{
  "tickets": [{
    "ticket_id": "atlas:T-1", "channel": "text",
    "gold_candidate_id": "GROUP-CSV",
    "items": [{"kind": "feature_request", "need_id": "csv", "occurrence_id": "O-1"}]
  }],
  "groups": [["O-1"]]
}
```

Results дополнительно задаёт provider=internal и complete=true/false на тикет, candidate_ids и evidence_valid=[true/false] на item. Evidence validity устанавливается независимым валидатором/разметчиком; не используйте самооценку модели как истину. Соответствие need_id также должно устанавливаться независимой разметкой, а не точным совпадением произвольных пересказов.

## Подсчёт

Extraction precision/recall сравнивает множества ticket/kind/need. Pairwise precision/recall сравнивает пары occurrence внутри групп. Candidate recall использует **gold_candidate_id из gold**; не найденные/отсутствующие result tickets остаются в знаменателе. Coverage по каналу — fully complete results / все gold tickets канала; дубли и смена scope не могут поднять его выше1.

Неизвестные числители/пустые знаменатели возвращаются null там, где процент не имеет смысла. `missing_result_tickets` показывает не представленные результаты. Тесты evaluator проверяют арифметику и целостность файлов, не дают оценку реальной модели.

```sh
.venv/bin/support-agent evaluate --gold /path/gold.json --results /path/internal-results.json
```

До получения реального набора extraction precision>=90%, recall>=80% и pairwise precision>=90% остаются целями пилота. Нельзя публиковать качество модели, рассчитанное на ответах того же mock-корпуса.

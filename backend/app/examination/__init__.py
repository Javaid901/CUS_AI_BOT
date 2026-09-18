"""backend/app/examination — dedicated Examinations services.

Model Papers, Exam Fee Structure and Division Improvement are served by their
own structured pipelines (detect.py / service.py / routes.py), never the
generic RAG loop. Facts come only from verified official corpus rows or an
honest not-available message.
"""
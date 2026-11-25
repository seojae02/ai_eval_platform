from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('competitions', '0003_project_deadline_projectfile'),
    ]

    operations = [
        migrations.AddField(
            model_name='projectfile',
            name='duplicate_rows',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='특성이 같은 행 수'),
        ),
    ]

# Generated by Django 3.2.4 on 2022-03-22 20:47

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workflow', '0049_merge_0045_state_is_allow_skip_0048_alter_state_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notify',
            name='type',
            field=models.CharField(default='EMAIL', max_length=32, verbose_name='通知渠道'),
        ),
    ]

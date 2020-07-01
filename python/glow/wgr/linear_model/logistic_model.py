# Copyright 2019 The Glow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from .logistic_udfs import *
from nptyping import Float, NDArray
import pandas as pd
from pyspark.sql import DataFrame, Row
from pyspark.sql.functions import pandas_udf, PandasUDFType
import pyspark.sql.functions as f
from pyspark.sql.window import Window
from typeguard import typechecked
from typing import Any, Dict, List
from glow.logging import record_hls_event

@typechecked
class LogisticRegression:
    def __init__(self, alphas: NDArray[(Any, ), Float] = np.array([])) -> None:
        if not (alphas >= 0).all():
            raise Exception('Alpha values must all be non-negative.')
        self.alphas = create_alpha_dict(alphas)

    def fit(self, blockdf: DataFrame, labeldf: pd.DataFrame, sample_blocks: Dict[str, List[str]],
            covdf: pd.DataFrame) -> (DataFrame, DataFrame):
        map_key_pattern = ['sample_block', 'label', 'alpha_name']
        reduce_key_pattern = ['header_block', 'header', 'label', 'alpha_name']
        model_key_pattern = ['sample_block', 'label']
        p0_dict = {k : v for k, v in zip(labeldf.columns, labeldf.sum(axis = 0)/labeldf.shape[0])}

        if not self.alphas:
            self.alphas = generate_alphas(blockdf)

        map_udf = pandas_udf(
            lambda key, pdf: map_irls_eqn(key, map_key_pattern, pdf, labeldf, sample_blocks, covdf, p0_dict, self.alphas),
            irls_eqn_struct,
            PandasUDFType.GROUPED_MAP
        )

        reduce_udf = pandas_udf(
            lambda key, pdf: reduce_irls_eqn(key, reduce_key_pattern, pdf),
            irls_eqn_struct,
            PandasUDFType.GROUPED_MAP
        )

        model_udf = pandas_udf(
            lambda key, pdf: solve_irls_eqn(key, model_key_pattern, pdf, labeldf, self.alphas, covdf),
            model_struct,
            PandasUDFType.GROUPED_MAP
        )

        score_udf = pandas_udf(
            lambda key, pdf: score_models(key, model_key_pattern, pdf, labeldf, sample_blocks, self.alphas, covdf, metric = 'log_loss'),
            cv_struct,
            PandasUDFType.GROUPED_MAP
        )

        modeldf = blockdf \
            .withColumn('alpha_name', f.explode(f.array([f.lit(n) for n in self.alphas.keys()]))) \
            .groupBy(map_key_pattern) \
            .apply(map_udf) \
            .groupBy(reduce_key_pattern) \
            .apply(reduce_udf) \
            .groupBy(model_key_pattern) \
            .apply(model_udf)

        alpha_df = blockdf.sql_ctx \
            .createDataFrame([Row(alpha=k, alpha_value=float(v)) for k, v in self.alphas.items()])

        window_spec = Window.partitionBy('label').orderBy('log_loss_mean', f.desc('alpha_value'))

        cvdf = blockdf.drop('header_block', 'sort_key') \
            .join(modeldf, ['header', 'sample_block'], 'right') \
            .withColumn('label', f.coalesce(f.col('label'), f.col('labels').getItem(0))) \
            .groupBy(model_key_pattern) \
            .apply(score_udf) \
            .join(alpha_df, ['alpha']) \
            .groupBy('label', 'alpha', 'alpha_value').agg(f.mean('score').alias('log_loss_mean')) \
            .withColumn('modelRank', f.row_number().over(window_spec)) \
            .filter('modelRank = 1') \
            .drop('modelRank')

        return modeldf, cvdf

    def reduce_block_matrix(self, blockdf: DataFrame, labeldf: pd.DataFrame, sample_blocks: Dict[str, List[str]],
                            modeldf: DataFrame, cvdf: DataFrame, covdf: pd.DataFrame, response: str) -> DataFrame:
        transform_key_pattern = ['sample_block', 'label']

        if response == 'linear':
            transform_udf = pandas_udf(
                lambda key, pdf: apply_model(key, transform_key_pattern, pdf, labeldf, sample_blocks, self.alphas, covdf),
                reduced_matrix_struct,
                PandasUDFType.GROUPED_MAP
            )
            join_type = 'inner'
        elif response == 'sigmoid':
            transform_udf = pandas_udf(
                lambda key, pdf: apply_logistic_model(key, transform_key_pattern, pdf, labeldf, sample_blocks, self.alphas, covdf),
                logistic_reduced_matrix_struct,
                PandasUDFType.GROUPED_MAP
            )
            join_type = 'right'
        else:
            raise Exception(f'response must be either "linear" or "sigmoid", received "{response}"')

        return blockdf.drop('header_block', 'sort_key') \
            .join(modeldf.drop('header_block'), ['sample_block', 'header'], join_type) \
            .withColumn('label', f.coalesce(f.col('label'), f.col('labels').getItem(0))) \
            .groupBy(transform_key_pattern) \
            .apply(transform_udf) \
            .join(cvdf, ['label', 'alpha'], 'inner')

    def transform(self, blockdf: DataFrame, labeldf: pd.DataFrame, sample_blocks: Dict[str, List[str]],
                  modeldf: DataFrame, cvdf: DataFrame) -> pd.DataFrame:

        block_prediction_df = self.reduce_block_matrix(blockdf, labeldf, sample_blocks, modeldf, cvdf, covdf = pd.DataFrame({}), response = 'linear')
        pivoted_df = flatten_prediction_df(block_prediction_df, sample_blocks, labeldf)

        return pivoted_df

    def predict_proba(self, blockdf: DataFrame, labeldf: pd.DataFrame, sample_blocks: Dict[str, List[str]],
                      modeldf: DataFrame, cvdf: DataFrame, covdf: DataFrame) -> pd.DataFrame:

        block_prediction_df = self.reduce_block_matrix(blockdf, labeldf, sample_blocks, modeldf, cvdf, covdf, response = 'sigmoid')
        pivoted_df = flatten_prediction_df(block_prediction_df, sample_blocks, labeldf)

        return pivoted_df


    def fit_transform(self, blockdf: DataFrame, labeldf: pd.DataFrame, sample_blocks: Dict[str, List[str]],
                      covdf: pd.DataFrame) -> pd.DataFrame:

        modeldf, cvdf = self.fit(blockdf, labeldf, sample_blocks, covdf)
        return self.transform(blockdf, labeldf, sample_blocks, modeldf, cvdf)
